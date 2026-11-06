import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def workflow_run_block(workflow, step_name):
    """Read one literal run block without consuming the next step or job."""
    lines = workflow.splitlines()
    position = lines.index("      - name: " + step_name)
    for index in range(position + 1, len(lines)):
        line = lines[index]
        if line == "        run: |":
            start = index + 1
            break
        if line.strip() and len(line) - len(line.lstrip()) <= 6:
            raise ValueError("Expected a literal run block")
    else:
        raise ValueError("Expected a literal run block")
    stop = next(
        (index for index in range(start, len(lines)) if lines[index] and not lines[index].startswith("          ")),
        len(lines),
    )
    return "\n".join(lines[start:stop]) + "\n"


class DeploymentSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.app = self.root / "app"
        self.app.mkdir()
        (self.app / ".git").mkdir()
        scripts = self.app / "scripts"
        scripts.mkdir()
        self.capture = self.root / "remote-command"
        self.log = self.root / "commands"
        self.workflow = (ROOT / ".github/workflows/deploy-dashboard.yml").read_text()
        self.command = textwrap.dedent(workflow_run_block(self.workflow, "Update, refresh, and publish dashboard"))
        self.env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            CAPTURE_FILE=str(self.capture),
            COMMAND_LOG=str(self.log),
            CPANEL_APP_PATH=str(self.app),
            CPANEL_DASHBOARD_PATH=str(self.root / "protected"),
            CPANEL_SSH_HOST="synthetic.invalid",
            CPANEL_SSH_USERNAME="synthetic",
            CPANEL_SSH_PORT="",
            DEPLOY_SHA="a" * 40,
            EXPECTED_SHA="a" * 40,
            SYNC_MARKER=str(self.root / "sync-marker"),
            SYNC_ARGUMENTS=str(self.root / "sync-arguments"),
            REFRESH_ARGUMENTS=str(self.root / "refresh-arguments"),
            RELEASE_SHA_CAPTURE=str(self.root / "release-sha"),
        )
        # Exercise util-linux flock in CI. macOS uses the same kernel-lock
        # semantics through its standard-library fcntl module.
        self.flock = shutil.which("flock")
        if not self.flock:
            self.executable(
                self.bin / "flock",
                f"#!{sys.executable}\nimport fcntl,sys\nfcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX)\n",
            )
        self.executable(
            self.bin / "ssh",
            '#!/bin/sh\nfor last do :; done\nprintf "%s\\n" "$last" > "$CAPTURE_FILE"\n',
        )
        self.executable(
            self.bin / "git",
            """#!/bin/sh
printf "%s\\n" "$*" >> "$COMMAND_LOG"
case "$1" in
status) [ "$SCENARIO" = dirty ] && printf " M synthetic\\n"; exit 0;;
branch) [ "$SCENARIO" = wrong_branch ] && printf "feature\\n" || printf "main\\n";;
fetch)
  if [ "$SCENARIO" = post_sync_fetch_failure ] && [ -f "$SYNC_MARKER" ]; then exit 1; fi;;
rev-parse)
  if [ "$2" = origin/main ] && { [ "$SCENARIO" = stale ] ||
      { [ "$SCENARIO" = moves_during_sync ] && [ -f "$SYNC_MARKER" ]; }; }; then
    printf "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\n"
  elif [ "$2" = HEAD ] && { [ "$SCENARIO" = wrong_head ] ||
      { [ "$SCENARIO" = head_moves_during_sync ] && [ -f "$SYNC_MARKER" ]; }; }; then
    printf "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\n"
  else printf "%s\\n" "$EXPECTED_SHA"; fi;;
merge) [ "$SCENARIO" != divergent ];;
*) exit 99;;
esac
""",
        )
        self.executable(
            scripts / "start-energy.sh",
            '#!/bin/sh\nprintf "SYNC\\n" >> "$COMMAND_LOG"\n'
            'printf "%s\\n" "$@" > "$SYNC_ARGUMENTS"\n: > "$SYNC_MARKER"\n[ "$SCENARIO" != sync_failure ]\n',
        )
        self.executable(
            scripts / "publish-energy-dashboard.sh",
            '#!/bin/sh\nprintf "PUBLISH\\n" >> "$COMMAND_LOG"\n',
        )
        self.executable(
            scripts / "install-energy-refresh.sh",
            '#!/bin/sh\nprintf "INSTALL\\n" >> "$COMMAND_LOG"\n[ "$SCENARIO" != install_failure ]\n',
        )
        # The runner owns the post-collection release check before publishing.
        # Its real implementation is exercised by the runner tests.
        self.executable(
            scripts / "refresh-energy.sh",
            """#!/bin/sh
printf "%s\\n" "$@" > "$REFRESH_ARGUMENTS"
printf "%s\\n" "$ENERGY_REFRESH_EXPECTED_SHA" > "$RELEASE_SHA_CAPTURE"
./scripts/start-energy.sh --days 90 --gas-units kwh &&
git fetch --no-tags origin main &&
if [ "$(git rev-parse origin/main)" != "$ENERGY_REFRESH_EXPECTED_SHA" ]; then
  echo 'origin/main changed during collection; skipping publication.'; exit 0
fi &&
test "$(git rev-parse HEAD)" = "$ENERGY_REFRESH_EXPECTED_SHA" &&
./scripts/publish-energy-dashboard.sh
""",
        )
        # Match the GitHub Actions default Bash runner's fail-fast behavior.
        result = subprocess.run(
            ["bash", "-e", "-c", self.command], env=self.env, capture_output=True, text=True, timeout=5
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.capture.is_file())
        self.remote = self.capture.read_text()

    @staticmethod
    def executable(path, body):
        path.write_text(body)
        path.chmod(0o755)

    def assert_scenario(self, scenario, expected_code, *, sync=False, publish=False):
        self.log.write_text("")
        result = subprocess.run(
            ["sh", "-c", self.remote],
            env=dict(self.env, SCENARIO=scenario),
            capture_output=True,
            text=True,
            timeout=5,
        )
        commands = self.log.read_text().splitlines()
        self.assertEqual(result.returncode, expected_code, result.stderr)
        self.assertEqual("SYNC" in commands, sync)
        self.assertEqual("PUBLISH" in commands, publish)
        self.assertFalse(any("reset" in command for command in commands))
        return commands

    def test_valid_tested_commit_fast_forwards_then_syncs_and_publishes(self):
        commands = self.assert_scenario("valid", 0, sync=True, publish=True)
        self.assertIn("merge --ff-only " + self.env["EXPECTED_SHA"], commands)
        self.assertEqual(
            Path(self.env["SYNC_ARGUMENTS"]).read_text().splitlines(),
            ["--days", "90", "--gas-units", "kwh"],
        )
        self.assertLess(commands.index("SYNC"), commands.index("PUBLISH"))
        self.assertLess(commands.index("INSTALL"), commands.index("SYNC"))
        self.assertEqual(
            Path(self.env["REFRESH_ARGUMENTS"]).read_text().splitlines(),
            ["--force", "--inherited-lock-fd", "9"],
        )
        self.assertEqual(Path(self.env["RELEASE_SHA_CAPTURE"]).read_text().strip(), self.env["EXPECTED_SHA"])

    def test_failed_cron_installation_never_starts_collection(self):
        self.assert_scenario("install_failure", 1)

    def test_dirty_checkout_never_updates_or_publishes(self):
        commands = self.assert_scenario("dirty", 1)
        self.assertEqual(commands, ["status --porcelain"])

    def test_non_main_checkout_never_updates_or_publishes(self):
        commands = self.assert_scenario("wrong_branch", 1)
        self.assertFalse(any(command.startswith("fetch") for command in commands))

    def test_superseded_tested_commit_skips_without_updating_or_publishing(self):
        commands = self.assert_scenario("stale", 0)
        self.assertFalse(any(command.startswith("merge") for command in commands))

    def test_divergent_checkout_never_syncs_or_publishes(self):
        self.assert_scenario("divergent", 1)

    def test_checkout_sha_mismatch_never_syncs_or_publishes(self):
        self.assert_scenario("wrong_head", 1)

    def test_failed_sync_never_publishes(self):
        self.assert_scenario("sync_failure", 1, sync=True)

    def test_failed_sync_releases_lock_for_next_deployment(self):
        self.assert_scenario("sync_failure", 1, sync=True)
        self.assert_scenario("valid", 0, sync=True, publish=True)

    def test_main_moving_during_sync_never_publishes_obsolete_snapshot(self):
        self.assert_scenario("moves_during_sync", 0, sync=True)

    def test_failed_post_sync_fetch_never_publishes(self):
        self.assert_scenario("post_sync_fetch_failure", 1, sync=True)

    def test_head_moving_during_sync_never_publishes(self):
        self.assert_scenario("head_moves_during_sync", 1, sync=True)

    def test_missing_flock_fails_before_checkout_or_publication(self):
        (self.bin / "flock").unlink(missing_ok=True)
        self.log.write_text("")
        result = subprocess.run(
            ["/bin/sh", "-c", self.remote],
            env={**self.env, "PATH": str(self.bin)},
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.log.read_text(), "")

    def test_missing_git_directory_fails_before_checkout_or_publication(self):
        (self.app / ".git").rmdir()
        self.assert_scenario("valid", 1)

    def test_lock_failure_fails_before_checkout_or_publication(self):
        self.executable(self.bin / "flock", "#!/bin/sh\nexit 73\n")
        self.assertEqual(self.assert_scenario("valid", 73), [])

    def test_invalid_configuration_never_reaches_ssh(self):
        for key, value in (
            ("CPANEL_APP_PATH", "/tmp/unsafe;command"),
            ("CPANEL_DASHBOARD_PATH", "/tmp/unsafe path"),
            ("DEPLOY_SHA", "not-a-tested-sha"),
            ("CPANEL_SSH_PORT", "22;command"),
            ("ENERGY_REFRESH_INTERVAL_MINUTES", "0"),
            ("ENERGY_REFRESH_INTERVAL_MINUTES", "60;command"),
        ):
            with self.subTest(key=key):
                self.capture.unlink(missing_ok=True)
                result = subprocess.run(
                    ["bash", "-e", "-c", self.command],
                    env={**self.env, key: value},
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())

    def test_optional_ssh_port_defaults_and_custom_value_reach_ssh(self):
        arguments = self.root / "ssh-arguments"
        self.executable(self.bin / "ssh", '#!/bin/sh\nprintf "%s\\n" "$@" > "$ARG_CAPTURE"\n')
        for supplied, expected in ((None, "22"), ("", "22"), ("2222", "2222")):
            with self.subTest(port=supplied):
                env = {**self.env, "ARG_CAPTURE": str(arguments)}
                env.pop("CPANEL_SSH_PORT", None)
                if supplied is not None:
                    env["CPANEL_SSH_PORT"] = supplied
                result = subprocess.run(
                    ["bash", "-e", "-c", self.command], env=env, capture_output=True, text=True, timeout=5
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                args = arguments.read_text().splitlines()
                self.assertEqual(args[args.index("-p") + 1], expected)
                self.assertIn("StrictHostKeyChecking=yes", args)
                self.assertIn("BatchMode=yes", args)

    def test_only_ready_successful_pushes_enter_deployment_queue(self):
        preflight, deploy = self.workflow.split("\n  deploy:\n", 1)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", preflight)
        self.assertIn("github.event.workflow_run.event == 'push'", preflight)
        self.assertIn("github.event.workflow_run.head_repository.full_name == github.repository", preflight)
        self.assertIn("needs: preflight", deploy)
        self.assertIn("if: needs.preflight.outputs.ready == 'true'", deploy)
        group = deploy.split("group: ", 1)[1].splitlines()[0]
        expression = "${{ github.event.workflow_run.head_sha }}"
        self.assertEqual(group, "deploy-dashboard-" + expression)
        # A late old success cannot occupy the current commit's pending slot.
        self.assertEqual(len({group.replace(expression, letter * 40) for letter in "cba"}), 3)
        self.assertIn("cancel-in-progress: false", deploy)
        self.assertNotIn("concurrency:", preflight)

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)

    def wait_for(self, path):
        deadline = time.monotonic() + 5
        while not path.exists():
            if time.monotonic() >= deadline:
                self.fail("Concurrent deployment did not reach " + path.name)
            time.sleep(0.01)

    def assert_concurrent_deployments(self, waiter_order):
        state = self.root / "concurrent"
        state.mkdir()
        (state / "origin").write_text("c" * 40)
        (state / "head").write_text("d" * 40)
        events = state / "events"
        events.write_text("")
        support = f"#!{sys.executable}\n" + textwrap.dedent("""\
            import os,sys,time
            from pathlib import Path
            state = Path(os.environ["CONCURRENT_STATE"])
            label = os.environ["RUN_LABEL"]
            def record(command):
                with (state / "events").open("a") as stream:
                    stream.write(label + "|" + command + "\\n")
            """)
        self.executable(
            self.bin / "git",
            support
            + textwrap.dedent("""\
                args = sys.argv[1:]
                record(" ".join(args))
                if args[0] == "branch":
                    print("main")
                elif args[0] == "rev-parse":
                    print((state / ("origin" if args[1] == "origin/main" else "head")).read_text())
                elif args[0] == "merge":
                    (state / "head").write_text(args[-1])
                elif args[0] not in {"fetch", "status"}:
                    sys.exit(99)
                """),
        )
        self.executable(
            self.app / "scripts/start-energy.sh",
            support
            + textwrap.dedent("""\
                record("SYNC_START")
                (state / ("sync-" + label)).touch()
                deadline = time.monotonic() + 10
                while not (state / ("release-" + label)).exists():
                    if time.monotonic() >= deadline:
                        sys.exit(98)
                    time.sleep(0.01)
                record("SYNC_END")
                """),
        )
        self.executable(
            self.app / "scripts/publish-energy-dashboard.sh",
            support + 'record("PUBLISH " + (state / "head").read_text())\n',
        )
        lock_program = support + '(state / ("waiting-" + label)).touch()\n'
        if self.flock:
            lock_program += f"os.execv({self.flock!r}, [{self.flock!r}, *sys.argv[1:]])\n"
        else:
            lock_program += "import fcntl\nfcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX)\n"
        self.executable(self.bin / "flock", lock_program)

        def launch(label):
            capture = state / ("remote-" + label)
            env = {
                **self.env,
                "CAPTURE_FILE": str(capture),
                "CONCURRENT_STATE": str(state),
                "RUN_LABEL": label,
                "DEPLOY_SHA": label.lower() * 40,
            }
            captured = subprocess.run(
                ["bash", "-e", "-c", self.command], env=env, capture_output=True, text=True, timeout=5
            )
            self.assertEqual(captured.returncode, 0, captured.stderr)
            process = subprocess.Popen(
                ["sh", "-c", capture.read_text()],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            self.addCleanup(self.stop_process, process)
            return process

        processes = [launch("C")]
        self.wait_for(state / "sync-C")
        (state / "origin").write_text("b" * 40)
        for label in waiter_order:
            processes.append(launch(label))
            self.wait_for(state / ("waiting-" + label))
        self.assertEqual({line.split("|")[0] for line in events.read_text().splitlines()}, {"C"})
        (state / "release-C").touch()
        self.wait_for(state / "sync-B")
        (state / "release-B").touch()
        for process in processes:
            _, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error)
        lines = events.read_text().splitlines()
        labels = [line.split("|")[0] for line in lines]
        sections = [label for index, label in enumerate(labels) if index == 0 or label != labels[index - 1]]
        self.assertEqual(sections[0], "C")
        self.assertEqual(len(sections), 3, "Checkout/sync/publication sections must never interleave")
        self.assertEqual(set(sections), {"A", "B", "C"})
        self.assertEqual([line for line in lines if "|PUBLISH " in line], ["B|PUBLISH " + "b" * 40])
        self.assertFalse(any(line.startswith("A|merge") for line in lines))
        self.assertEqual((state / "head").read_text(), "b" * 40)

    def test_current_release_survives_older_late_waiter(self):
        self.assert_concurrent_deployments(("B", "A"))

    def test_current_release_survives_older_early_waiter(self):
        self.assert_concurrent_deployments(("A", "B"))
