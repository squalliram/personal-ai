import os
import subprocess
import tempfile
import textwrap
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
            DEPLOY_SHA="a" * 40,
            EXPECTED_SHA="a" * 40,
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
fetch) exit 0;;
rev-parse)
  if [ "$2" = origin/main ] && [ "$SCENARIO" = stale ]; then
    printf "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\n"
  elif [ "$2" = HEAD ] && [ "$SCENARIO" = wrong_head ]; then
    printf "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\n"
  else printf "%s\\n" "$EXPECTED_SHA"; fi;;
merge) [ "$SCENARIO" != divergent ];;
*) exit 99;;
esac
""",
        )
        self.executable(
            scripts / "start-energy.sh",
            '#!/bin/sh\nprintf "SYNC\\n" >> "$COMMAND_LOG"\n[ "$SCENARIO" != sync_failure ]\n',
        )
        self.executable(
            scripts / "publish-energy-dashboard.sh",
            '#!/bin/sh\nprintf "PUBLISH\\n" >> "$COMMAND_LOG"\n',
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
        self.assertLess(commands.index("SYNC"), commands.index("PUBLISH"))

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

    def test_invalid_configuration_never_reaches_ssh(self):
        for key, value in (
            ("CPANEL_APP_PATH", "/tmp/unsafe;command"),
            ("CPANEL_DASHBOARD_PATH", "/tmp/unsafe path"),
            ("DEPLOY_SHA", "not-a-tested-sha"),
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

    def test_only_ready_successful_pushes_enter_deployment_queue(self):
        preflight, deploy = self.workflow.split("\n  deploy:\n", 1)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", preflight)
        self.assertIn("github.event.workflow_run.event == 'push'", preflight)
        self.assertIn("github.event.workflow_run.head_repository.full_name == github.repository", preflight)
        self.assertIn("needs: preflight", deploy)
        self.assertIn("if: needs.preflight.outputs.ready == 'true'", deploy)
        self.assertIn("group: deploy-dashboard", deploy)
        self.assertIn("cancel-in-progress: false", deploy)
        self.assertNotIn("concurrency:", preflight)
