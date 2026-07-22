from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys

from .benchmark import load_records, load_task_plan, render as render_benchmark, verify_task_commits
from .config import load_config, validate, load_toml, warnings as config_warnings
from .doctor import doctor, render as render_doctor
from .errors import HarnessError, SupervisorDiedError
from .files import atomic_write
from .hooks import run_hook
from .installer import install, uninstall
from .inventory import create_backup, inventory
from .maintenance import cleanup
from .paths import source_root, user_paths
from .runner import delegate, retry, start_detached_delegate, wait_for_run
from .taskfile import create_task_file
from .trust import confirm_codex_hook
from .watch import watch


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="cross-harness")
    root.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    commands = root.add_subparsers(dest="command", required=True)

    validate_parser = commands.add_parser("validate", help="validate personal configuration")
    validate_parser.add_argument("--config", type=Path)

    inventory_parser = commands.add_parser("inventory", help="record versions, auth status, and setting hashes")
    inventory_parser.add_argument("--output", type=Path)
    inventory_parser.add_argument("--backup", type=Path)

    install_parser = commands.add_parser("install", help="back up and install user assets")
    install_parser.add_argument("--repo", type=Path, default=source_root())
    install_parser.add_argument("--dry-run", action="store_true")
    install_parser.add_argument("--force", action="store_true", help="overwrite changed installed files")

    uninstall_parser = commands.add_parser("uninstall", help="restore the pre-install state")
    uninstall_parser.add_argument("--force", action="store_true")
    uninstall_parser.add_argument("--preserve-user-changes", action="store_true")
    uninstall_parser.add_argument("--purge-runtime", action="store_true")

    delegate_parser = commands.add_parser("delegate", help="run a bounded delegated task")
    delegate_parser.add_argument("--role", required=True)
    delegate_parser.add_argument("--kind", required=True)
    delegate_parser.add_argument("--task-file", required=True, type=Path)
    delegate_parser.add_argument("--cwd", required=True, type=Path)
    delegate_parser.add_argument("--config", type=Path)
    delegate_parser.add_argument("--confirm-high-risk", action="store_true")
    delegate_parser.add_argument("--no-detach", action="store_true", help="run in this process")
    delegate_parser.add_argument("--timeout-seconds", type=float, default=86400, help="foreground wait limit")
    delegate_parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    delegate_parser.add_argument("--supervisor", action="store_true", help=argparse.SUPPRESS)

    wait_parser = commands.add_parser("wait", help="wait for a delegated run to finalize")
    wait_parser.add_argument("--run", required=True, type=Path)
    wait_parser.add_argument("--timeout-seconds", required=True, type=float)

    watch_parser = commands.add_parser("watch", help="follow the newest delegated run")
    watch_parser.add_argument("--config", type=Path)
    watch_parser.add_argument("--all", action="store_true", help="include lifecycle events")
    watch_parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")

    retry_parser = commands.add_parser("retry", help="resume a failed task with a delta instruction")
    retry_parser.add_argument("--run-dir", required=True, type=Path)
    retry_parser.add_argument("--task-file", required=True, type=Path)
    retry_parser.add_argument("--config", type=Path)

    task_parser = commands.add_parser("task", help="create a credential-screened delegation task file")
    task_commands = task_parser.add_subparsers(dest="task_command", required=True)
    task_create = task_commands.add_parser("create", help="create a task file in the runtime inbox")
    task_create.add_argument("--role", required=True)
    task_create.add_argument("--kind", required=True)
    task_create.add_argument("--cwd", required=True, type=Path)
    task_create.add_argument("--goal", required=True)
    task_create.add_argument("--done-when", required=True, action="append")
    task_create.add_argument("--scope", action="append", default=[])
    task_create.add_argument("--constraint", action="append", default=[])
    task_create.add_argument("--check", action="append", default=[])
    task_create.add_argument("--reference", action="append", default=[])
    task_create.add_argument("--assumption", action="append", default=[])
    task_create.add_argument("--config", type=Path)

    hook_parser = commands.add_parser("hook", help=argparse.SUPPRESS)
    hook_parser.add_argument("name")

    cleanup_parser = commands.add_parser("cleanup", help="mark or remove stale runtime directories")
    cleanup_parser.add_argument("--config", type=Path)

    doctor_parser = commands.add_parser("doctor", help="check installation, auth, and guards")
    doctor_parser.add_argument("--config", type=Path)
    doctor_parser.add_argument("--json", action="store_true")

    trust_parser = commands.add_parser("trust", help="record an explicitly reviewed native trust decision")
    trust_commands = trust_parser.add_subparsers(dest="trust_command", required=True)
    trust_codex_hook = trust_commands.add_parser("codex-hook", help="record review of the current Codex hook definition")
    trust_codex_hook.add_argument("--confirmed-after-review", action="store_true")

    benchmark_parser = commands.add_parser("benchmark", help="validate and summarize a 5x2 benchmark")
    benchmark_parser.add_argument("--input", required=True, type=Path)
    benchmark_parser.add_argument("--output", required=True, type=Path)

    benchmark_plan_parser = commands.add_parser("benchmark-plan", help="validate five reproducible benchmark tasks and commits")
    benchmark_plan_parser.add_argument("--input", required=True, type=Path)
    benchmark_plan_parser.add_argument("--repo", required=True, type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    home = args.home
    try:
        if args.command == "validate":
            config_path = args.config or user_paths(home).config
            config = load_toml(config_path) if config_path.exists() else load_config(home=home)
            errors = validate(config)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 2
            for warning in config_warnings(config):
                print(f"warning: {warning}", file=sys.stderr)
            print("configuration valid")
        elif args.command == "inventory":
            report = inventory(user_paths(home))
            if args.output:
                atomic_write(args.output, report, 0o600)
                print(args.output)
            else:
                print(report, end="")
            if args.backup:
                copied = create_backup(args.backup, user_paths(home))
                print(f"backed up {len(copied)} setting files to {args.backup}")
        elif args.command == "install":
            for action in install(home, args.repo, args.dry_run, args.force):
                print(action)
        elif args.command == "uninstall":
            restored = uninstall(home, args.force, args.preserve_user_changes, args.purge_runtime)
            print(f"restored {len(restored)} paths")
        elif args.command == "delegate":
            if args.supervisor or args.no_detach:
                summary = delegate(
                    args.role,
                    args.kind,
                    args.task_file.resolve(),
                    args.cwd.resolve(),
                    args.config,
                    home,
                    args.confirm_high_risk,
                    args.run_dir.resolve() if args.run_dir else None,
                )
                print((Path(summary["run_dir"]) / "summary.txt").read_text(encoding="utf-8"), end="")
                return 0 if summary["status"] == "success" else 1
            run_dir = start_detached_delegate(
                args.role,
                args.kind,
                args.task_file.resolve(),
                args.cwd.resolve(),
                args.config,
                home,
                args.confirm_high_risk,
            )
            print(run_dir, flush=True)
            print("watch progress: cross-harness watch", file=sys.stderr, flush=True)
            summary = wait_for_run(run_dir, args.timeout_seconds)
            if summary is None:
                return 4
            print((run_dir / "summary.txt").read_text(encoding="utf-8"), end="")
            return 0 if summary["status"] == "success" else 3
        elif args.command == "wait":
            summary = wait_for_run(args.run.resolve(), args.timeout_seconds)
            if summary is None:
                return 4
            print((args.run.resolve() / "summary.txt").read_text(encoding="utf-8"), end="")
            return 0 if summary["status"] == "success" else 3
        elif args.command == "watch":
            return watch(args.config, home, show_all=args.all, color=args.color)
        elif args.command == "retry":
            summary = retry(args.run_dir.resolve(), args.task_file.resolve(), args.config, home)
            print((Path(summary["run_dir"]) / "summary.txt").read_text(encoding="utf-8"), end="")
            return 0 if summary["status"] == "success" else 1
        elif args.command == "task":
            if args.task_command == "create":
                path = create_task_file(
                    args.role,
                    args.kind,
                    args.cwd.resolve(),
                    args.goal,
                    args.done_when,
                    args.scope,
                    args.constraint,
                    args.check,
                    args.reference,
                    args.assumption,
                    args.config,
                    home,
                )
                print(path)
        elif args.command == "hook":
            return run_hook(args.name, home)
        elif args.command == "cleanup":
            print(json.dumps(cleanup(args.config, home), ensure_ascii=False, indent=2))
        elif args.command == "doctor":
            report = doctor(home, args.config)
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_doctor(report), end="")
            return 0 if report["ok"] else 1
        elif args.command == "trust":
            if args.trust_command == "codex-hook":
                print(confirm_codex_hook(home, confirmed_after_review=args.confirmed_after_review))
        elif args.command == "benchmark":
            report = render_benchmark(load_records(args.input))
            atomic_write(args.output, report, 0o644)
            print(args.output)
        elif args.command == "benchmark-plan":
            tasks = load_task_plan(args.input)
            for verified in verify_task_commits(tasks, args.repo.resolve()):
                print(verified)
        return 0
    except SupervisorDiedError as exc:
        print(f"cross-harness: {exc}", file=sys.stderr)
        return 5
    except HarnessError as exc:
        print(f"cross-harness: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
