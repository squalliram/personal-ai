import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from octopus_energy.dashboard import write_dashboard

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "octopus_energy/refresh.php"
REQUEST_A = "a" * 32
REQUEST_B = "b" * 32


class RefreshPHPStaticTests(unittest.TestCase):
    def setUp(self):
        self.source = TEMPLATE.read_text(encoding="utf-8")

    def test_export_copies_the_inert_template_verbatim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "dashboard"
            write_dashboard(root / "empty.sqlite3", output)
            self.assertEqual((output / "refresh.php").read_text(), self.source)
            html = (output / "index.html").read_text()
            self.assertIn('id="refresh" type="button">Fetch latest data', html)
            self.assertIn('id="refresh-status" role="status" aria-live="polite"', html)
            self.assertIn('data-period="latest-day"', html)
            self.assertNotIn("This page does not sync Octopus", html)

    def test_template_has_one_data_only_path_placeholder(self):
        self.assertEqual(self.source.count("__ENERGY_REFRESH_DIRECTORY_HEX__"), 1)
        self.assertIn("hex2bin(ENERGY_REFRESH_DIRECTORY_HEX)", self.source)
        self.assertIn("ctype_xdigit(ENERGY_REFRESH_DIRECTORY_HEX)", self.source)
        self.assertIn("$directory[0] !== '/'", self.source)
        self.assertIn("realpath($directory) !== $directory", self.source)
        self.assertIn("$directory . '/request.flag'", self.source)
        self.assertIn("$directory . '/request.running'", self.source)

    def test_request_protections_precede_flag_publication(self):
        creation = self.source.rindex("publish_refresh_flag($directory, $flag, $id)")
        for guard in [
            "['REQUEST_METHOD']",
            "'POST'",
            "'Allow: POST'",
            "['HTTPS']",
            "['REMOTE_USER']",
            "['REDIRECT_REMOTE_USER']",
            "'auth_unavailable'",
            "['HTTP_X_ENERGY_REFRESH']",
            "hash_equals('https://' . $host, $origin)",
            "['HTTP_SEC_FETCH_SITE']",
            "'same-origin'",
            "fileperms($directory)",
            "fileperms($path)",
            "is_link($path)",
        ]:
            self.assertLess(self.source.index(guard), creation)
        self.assertNotIn("HTTP_X_FORWARDED_PROTO", self.source)
        self.assertNotIn("$_SERVER['PHP_AUTH_USER']", self.source)

    def test_flag_is_exclusive_private_and_requests_are_coalesced(self):
        self.assertIn("umask(0077)", self.source)
        self.assertIn("!== 0700", self.source)
        self.assertIn("!== 0600", self.source)
        self.assertEqual(len(re.findall(r"\bfopen\s*\(", self.source)), 1)
        self.assertIn("refresh_reply(202, 'pending', 0,", self.source)
        self.assertIn("refresh_reply(202, 'already_running', 0,", self.source)
        self.assertIn("error_reporting() & $severity", self.source)
        self.assertNotRegex(self.source, r"\b(?:file_put_contents|touch|chmod|rename|mkdir|copy)\s*\(")
        self.assertIn("fopen($temporary, 'x')", self.source)
        self.assertIn("fwrite($handle, $id) !== 32", self.source)
        self.assertIn("!fflush($handle)", self.source)
        self.assertIn("return @link($temporary, $flag)", self.source)
        self.assertIn("finally {", self.source)
        self.assertEqual(len(re.findall(r"\bunlink\s*\(", self.source)), 1)
        self.assertIn("unlink($temporary)", self.source)
        self.assertIn("bin2hex(random_bytes(16))", self.source)

    def test_rate_limit_uses_durable_attempt_and_both_flag_timestamps(self):
        self.assertIn("ENERGY_REFRESH_COOLDOWN_SECONDS = 300", self.source)
        self.assertIn("$status['last_attempt_at']", self.source)
        self.assertRegex(self.source, r"max\(\$lastAttempt,.*filemtime\(\$flag\).*filemtime\(\$running\)")
        self.assertIn("refresh_reply(429, 'rate_limited'", self.source)
        self.assertIn("header('Retry-After: '", self.source)

    def test_active_worker_requires_running_status_and_retry_reply_binds_an_attempt(self):
        self.assertIn("$path === $running && $active && $matches", self.source)
        self.assertIn("$matches = $id === ($status['request_id'] ?? null)", self.source)
        self.assertIn("refresh_reply(202, 'retry_queued', 0, $status['last_attempt_at'], $id)", self.source)
        self.assertIn("$body['last_attempt_at'] = $attemptAt", self.source)
        self.assertIn("$body['request_id'] = $requestId", self.source)

    def test_complete_id_is_published_before_post_creation_watermark_read(self):
        main = self.source.rsplit("umask(0077);", 1)[1]
        self.assertLess(main.index("publish_refresh_flag("), main.index("$latest = read_refresh_status()"))
        self.assertIn("refresh_reply(202, 'pending', 0, $latest['last_attempt_at'] ?? null, $id)", main)
        publisher = self.source.split("function publish_refresh_flag(", 1)[1].split("set_error_handler", 1)[0]
        self.assertLess(publisher.index("fwrite($handle, $id)"), publisher.index("link($temporary, $flag)"))
        self.assertLess(publisher.index("fclose($handle)"), publisher.index("link($temporary, $flag)"))

    def test_selected_marker_is_resolved_once_more_only_when_it_vanished(self):
        resolver = self.source.split("function reply_existing_refresh(", 1)[1].split(
            "function publish_refresh_flag(", 1
        )[0]
        self.assertIn("array &$status", resolver)
        self.assertIn("$resolution < 2", resolver)
        self.assertIn("catch (RefreshFlagVanished $error)", resolver)
        self.assertIn("if ($resolution === 1)", resolver)
        self.assertIn("$status = read_refresh_status();", resolver)
        self.assertNotRegex(resolver, r"catch\s*\((?:RuntimeException|Throwable)")
        self.assertIn("$metadata['uid'] !== fileowner(dirname($path))", self.source)
        self.assertIn("@file_get_contents($path, false, null, 0, 33)", self.source)

    def test_no_source_sync_secrets_processes_or_detailed_error_output(self):
        self.assertNotRegex(
            self.source,
            r"\b(?:exec|shell_exec|system|passthru|proc_open|popen|pcntl_exec|eval|getenv|curl_init|include)\s*\(",
        )
        self.assertNotIn("$_ENV", self.source)
        self.assertNotIn("$_GET", self.source)
        self.assertNotIn("$_POST", self.source)
        self.assertNotIn(".env", self.source)
        self.assertNotRegex(self.source, r"echo\s+\$(?:error|directory|path|status)")
        self.assertIn("ini_set('display_errors', '0')", self.source)
        self.assertIn("catch (Throwable $error)", self.source)
        self.assertIn("header('Cache-Control: no-store')", self.source)


@unittest.skipUnless(
    shutil.which("php") or os.environ.get("ENERGY_REQUIRE_PHP") == "1",
    "PHP CLI unavailable locally; runtime endpoint checks run in CI",
)
class RefreshPHPRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("php"), "ENERGY_REQUIRE_PHP=1 requires the PHP CLI")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.private = self.root / "private refresh ' énergie"
        self.private.mkdir(mode=0o700)
        self.public = self.root / "public"
        self.public.mkdir()
        self.source = TEMPLATE.read_text().replace("__ENERGY_REFRESH_DIRECTORY_HEX__", str(self.private).encode().hex())
        self.endpoint = self.public / "refresh.php"
        self.endpoint.write_text(self.source)
        (self.public / "request.php").write_text(
            "<?php\n$_SERVER = json_decode(stream_get_contents(STDIN), true);\n"
            "register_shutdown_function(function () { fwrite(STDERR, json_encode(['code' => http_response_code()])); });\n"
            "require __DIR__ . '/refresh.php';\n"
        )
        self.server = {
            "REQUEST_METHOD": "POST",
            "HTTPS": "on",
            "REMOTE_USER": "synthetic-user",
            "HTTP_HOST": "private.test",
            "HTTP_ORIGIN": "https://private.test",
            "HTTP_X_ENERGY_REFRESH": "1",
            "HTTP_SEC_FETCH_SITE": "same-origin",
        }

    def request(self, **changes):
        server = {**self.server, **changes}
        server = {key: value for key, value in server.items() if value is not None}
        result = subprocess.run(
            ["php", str(self.public / "request.php")],
            input=json.dumps(server),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, "Endpoint process failed")
        self.assertNotIn(str(self.private), result.stdout + result.stderr)
        self.assertNotIn("synthetic-user", result.stdout + result.stderr)
        return json.loads(result.stderr)["code"], json.loads(result.stdout)

    def status(self, **changes):
        data = {
            "schema_version": 1,
            "last_attempt_at": None,
            "last_success_at": None,
            "result": None,
            "request_state": None,
            "latest_reading_at": {"electricity": None, "gas": None},
            "error_category": None,
            "request_id": None,
            **changes,
        }
        (self.public / "status.json").write_text(json.dumps(data))

    def install_link_race(self, mode):
        """Force a worker before/after exclusive publication, without production test hooks."""
        self.assertIn(mode, ("before-publication", "after-publication", "publication-failed"))
        fixture = r"""
namespace EnergyRefreshFixture;
use \RuntimeException;
use \Throwable;
function link(string $temporary, string $flag): bool
{
    $mode = '__CASE__';
    if ($mode === 'publication-failed') {
        return false;
    }
    $statusPath = __DIR__ . '/status.json';
    $status = json_decode(\file_get_contents($statusPath), true);
    $id = \file_get_contents($temporary);
    if (!preg_match('/\A[a-f0-9]{32}\z/', $id)) {
        throw new RuntimeException('Incomplete ID was exposed');
    }
    if ($mode === 'before-publication') {
        $status['last_attempt_at'] = '2026-01-01T01:00:00Z';
        $status['result'] = 'running';
        $status['request_state'] = 'running';
        $status['request_id'] = null;
        \file_put_contents($statusPath, json_encode($status));
        if (\is_file($flag)) {
            throw new RuntimeException('Flag unexpectedly present before publication');
        }
        $status['result'] = 'success';
        $status['request_state'] = null;
        $status['last_success_at'] = '2026-01-01T01:00:01Z';
        \file_put_contents($statusPath, json_encode($status));
        return \link($temporary, $flag);
    }
    $published = \link($temporary, $flag);
    if ($published) {
        $probe = ['id' => \file_get_contents($flag), 'mode' => \fileperms($flag) & 0777];
        \file_put_contents(__DIR__ . '/race-probe.json', json_encode($probe));
        $status['last_attempt_at'] = '2026-01-01T02:00:00Z';
        $status['result'] = 'running';
        $status['request_state'] = 'running';
        $status['request_id'] = null;
        \file_put_contents($statusPath, json_encode($status));
        $claimed = dirname($flag) . '/request.running';
        \rename($flag, $claimed);
        $status['request_id'] = $id;
        \file_put_contents($statusPath, json_encode($status));
        $status['result'] = 'success';
        $status['request_state'] = null;
        $status['last_success_at'] = '2026-01-01T02:00:01Z';
        \file_put_contents($statusPath, json_encode($status));
        \unlink($claimed);
    }
    return $published;
}
"""
        self.endpoint.write_text(
            self.source.replace(
                "declare(strict_types=1);", "declare(strict_types=1);\n" + fixture.replace("__CASE__", mode)
            )
        )

    def install_selection_race(self, mode):
        """Rename or invalidate a marker after selection, before its ID read."""
        self.assertIn(mode, ("rename", "completed", "vanish-twice", "permissions", "symlink", "owner", "read-denied"))
        fixture = r"""
namespace EnergyRefreshFixture;
use \RuntimeException;
use \Throwable;
function is_file(string $path): bool
{
    $exists = \is_file($path);
    $caller = debug_backtrace(DEBUG_BACKTRACE_IGNORE_ARGS, 2)[1]['function'] ?? '';
    if (!$exists || $caller !== __NAMESPACE__ . '\\reply_existing_refresh') {
        return $exists;
    }
    static $selections = 0;
    \file_put_contents(__DIR__ . '/selection-probe.json', json_encode(['selections' => ++$selections]));
    $mode = '__CASE__';
    if (basename($path) === 'request.running') {
        if ($mode === 'vanish-twice') {
            \unlink($path);
        }
        return $exists;
    }
    if ($mode === 'permissions') {
        \chmod($path, 0644);
    } elseif ($mode === 'symlink') {
        \unlink($path);
        \symlink(dirname($path) . '/request.running', $path);
    } elseif (in_array($mode, ['rename', 'completed', 'vanish-twice'], true)) {
        $statusPath = __DIR__ . '/status.json';
        $status = json_decode(\file_get_contents($statusPath), true);
        $id = \file_get_contents($path);
        $status['last_attempt_at'] = $mode === 'completed' ? gmdate('Y-m-d\TH:i:s\Z') : '2026-01-01T01:00:00.123456Z';
        $status['result'] = 'running';
        $status['request_state'] = 'running';
        $status['request_id'] = null;
        \file_put_contents($statusPath, json_encode($status));
        \rename($path, dirname($path) . '/request.running');
        $status['request_id'] = $id;
        \file_put_contents($statusPath, json_encode($status));
        if ($mode === 'completed') {
            $status['result'] = 'success';
            $status['request_state'] = null;
            $status['last_success_at'] = $status['last_attempt_at'];
            \file_put_contents($statusPath, json_encode($status));
            \unlink(dirname($path) . '/request.running');
        }
    }
    return $exists;
}
function stat(string $path)
{
    $metadata = \stat($path);
    if ('__CASE__' === 'owner' && basename($path) === 'request.flag' && is_array($metadata)) {
        $metadata['uid']++;
    }
    return $metadata;
}
function file_get_contents(string $path, ...$args)
{
    if ('__CASE__' === 'read-denied' && basename($path) === 'request.flag') {
        return false;
    }
    return \file_get_contents($path, ...$args);
}
"""
        self.endpoint.write_text(
            self.source.replace(
                "declare(strict_types=1);", "declare(strict_types=1);\n" + fixture.replace("__CASE__", mode)
            )
        )

    def test_rejects_get_http_cross_origin_missing_header_and_unverified_auth(self):
        for changes, expected in [
            ({"REQUEST_METHOD": "GET"}, 405),
            ({"HTTPS": "off", "HTTP_X_FORWARDED_PROTO": "https"}, 403),
            ({"HTTP_ORIGIN": "https://private.test.evil.test"}, 403),
            ({"HTTP_ORIGIN": None}, 403),
            ({"HTTP_X_ENERGY_REFRESH": None}, 403),
            ({"HTTP_SEC_FETCH_SITE": "cross-site"}, 403),
            ({"REMOTE_USER": None, "PHP_AUTH_USER": "synthetic-unverified"}, 503),
        ]:
            with self.subTest(changes=changes):
                code, body = self.request(**changes)
                self.assertEqual(code, expected)
                self.assertEqual(set(body), {"state", "retry_after"})
                if expected == 503:
                    self.assertEqual(body["state"], "auth_unavailable")
                self.assertFalse((self.private / "request.flag").exists())

    def test_creates_only_complete_private_id_flag_and_coalesces_without_replacement(self):
        code, body = self.request(REMOTE_USER=None, REDIRECT_REMOTE_USER="synthetic-user")
        self.assertEqual((code, body["state"]), (202, "pending"))
        flag = self.private / "request.flag"
        before = flag.stat()
        self.assertEqual(before.st_mode & 0o777, 0o600)
        self.assertRegex(body["request_id"], r"^[a-f0-9]{32}$")
        self.assertIsNone(body["last_attempt_at"])
        self.assertEqual(flag.read_bytes(), body["request_id"].encode("ascii"))
        second_code, second = self.request()
        self.assertEqual(second_code, 202)
        self.assertEqual(second["request_id"], body["request_id"])
        after = flag.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual({path.name for path in self.private.iterdir()}, {"request.flag"})

    def test_last_attempt_keeps_cooldown_after_flag_was_claimed_and_removed(self):
        self.status(last_attempt_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), result="failed")
        code, body = self.request()
        self.assertEqual((code, body["state"]), (429, "rate_limited"))
        self.assertGreaterEqual(body["retry_after"], 295)
        self.assertLessEqual(body["retry_after"], 300)
        self.assertFalse((self.private / "request.flag").exists())

    def test_claim_alone_is_queued_and_only_running_status_means_active(self):
        running = self.private / "request.running"
        running.touch(mode=0o600)
        before = running.stat()
        self.assertEqual(
            self.request(), (202, {"state": "pending", "retry_after": 0, "last_attempt_at": None, "request_id": None})
        )
        self.assertEqual(running.stat().st_mtime_ns, before.st_mtime_ns)
        running.unlink()
        self.status(result="running", last_attempt_at="2026-01-01T00:00:00Z")
        self.assertEqual(self.request()[1]["state"], "already_running")
        self.status(result="failed", request_state="running", last_attempt_at="2026-01-01T00:00:00Z")
        self.assertEqual(self.request()[1]["state"], "already_running", "explicit running state takes precedence")
        self.assertFalse((self.private / "request.flag").exists())

    def test_terminal_retry_acknowledges_exact_attempt_without_consuming_either_marker(self):
        attempt = "2026-01-01T00:00:00.123456Z"
        for name in ("request.flag", "request.running"):
            marker = self.private / name
            marker.write_text(REQUEST_A)
            marker.chmod(0o600)
            before = marker.stat()
            for outcome in ("no_data", "partial", "failed"):
                with self.subTest(marker=name, outcome=outcome):
                    self.status(result=outcome, request_state="pending", last_attempt_at=attempt, request_id=REQUEST_A)
                    code, body = self.request()
                    self.assertEqual(code, 202)
                    self.assertEqual(
                        body,
                        {
                            "state": "retry_queued",
                            "retry_after": 0,
                            "last_attempt_at": attempt,
                            "request_id": REQUEST_A,
                        },
                    )
                    self.assertEqual({path.name for path in self.private.iterdir()}, {name})
                    self.assertEqual(marker.read_bytes(), REQUEST_A.encode("ascii"))
                    self.assertEqual(
                        (marker.stat().st_ino, marker.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns)
                    )
            marker.unlink()

    def test_new_flag_with_unchanged_old_terminal_status_is_pending_not_completed(self):
        for outcome in ("no_data", "partial", "failed", "success"):
            with self.subTest(outcome=outcome):
                self.status(
                    result=outcome,
                    request_state="pending",
                    last_attempt_at="2026-01-01T00:00:00Z",
                    request_id=REQUEST_A,
                )
                status_before = (self.public / "status.json").read_bytes()
                code, body = self.request()
                self.assertEqual((code, body["state"]), (202, "pending"))
                self.assertEqual(body["last_attempt_at"], "2026-01-01T00:00:00Z")
                self.assertRegex(body["request_id"], r"^[a-f0-9]{32}$")
                self.assertEqual((self.private / "request.flag").read_text(), body["request_id"])
                self.assertEqual((self.public / "status.json").read_bytes(), status_before)
                (self.private / "request.flag").unlink()

    def test_success_with_queued_successor_is_not_active_or_acknowledged_as_failed_retry(self):
        flag = self.private / "request.flag"
        flag.touch(mode=0o600)
        before = flag.stat()
        self.status(result="success", request_state="pending", last_attempt_at="2026-01-01T00:00:00Z")
        self.assertEqual(
            self.request(),
            (
                202,
                {"state": "pending", "retry_after": 0, "last_attempt_at": "2026-01-01T00:00:00Z", "request_id": None},
            ),
        )
        self.assertEqual(flag.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertTrue(flag.exists())

    def test_rejects_symlinks_wrong_permissions_and_malformed_status(self):
        outside = self.root / "sentinel"
        outside.write_text("unchanged")
        flag = self.private / "request.flag"
        flag.symlink_to(outside)
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(outside.read_text(), "unchanged")
        flag.unlink()
        self.private.chmod(0o755)
        self.assertEqual(self.request()[0], 503)
        self.private.chmod(0o700)
        (self.public / "status.json").write_text("invalid JSON")
        self.assertEqual(self.request()[0], 503)
        self.assertFalse(flag.exists())

    def test_concurrent_requests_coalesce_to_one_private_flag(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            replies = list(executor.map(lambda _: self.request(), range(4)))
        self.assertEqual([code for code, _ in replies], [202] * 4)
        self.assertEqual(len({body["request_id"] for _, body in replies}), 1)
        self.assertRegex(replies[0][1]["request_id"], r"^[a-f0-9]{32}$")
        self.assertEqual({path.name for path in self.private.iterdir()}, {"request.flag"})
        self.assertEqual(os.stat(self.private / "request.flag").st_mode & 0o777, 0o600)

    def test_pending_b_does_not_coalesce_onto_retained_retry_a(self):
        flag, claimed = self.private / "request.flag", self.private / "request.running"
        for path, request_id in ((flag, REQUEST_B), (claimed, REQUEST_A)):
            path.write_text(request_id)
            path.chmod(0o600)
        self.status(
            result="failed", request_state="pending", request_id=REQUEST_A, last_attempt_at="2026-01-01T00:00:00Z"
        )
        code, body = self.request()
        self.assertEqual((code, body["state"], body["request_id"]), (202, "pending", REQUEST_B))
        self.assertEqual(flag.read_text(), REQUEST_B)
        self.assertEqual(claimed.read_text(), REQUEST_A)
        flag.unlink()
        code, body = self.request()
        self.assertEqual((code, body["state"], body["request_id"]), (202, "retry_queued", REQUEST_A))
        self.assertEqual(claimed.read_text(), REQUEST_A)

    def test_active_claim_acknowledges_its_id_and_server_attempt(self):
        claimed = self.private / "request.running"
        claimed.write_text(REQUEST_A)
        claimed.chmod(0o600)
        attempt = "2026-01-01T01:00:00Z"
        self.status(result="running", request_state="running", request_id=REQUEST_A, last_attempt_at=attempt)
        code, body = self.request()
        self.assertEqual((code, body["state"], body["request_id"]), (202, "already_running", REQUEST_A))
        self.assertEqual(body["last_attempt_at"], attempt)
        self.assertEqual(claimed.read_text(), REQUEST_A)

    def test_flag_renamed_after_selection_is_acknowledged_without_replacing_the_claim(self):
        flag, claimed = self.private / "request.flag", self.private / "request.running"
        flag.write_text(REQUEST_B)
        flag.chmod(0o600)
        before = flag.stat()
        self.status(result="success", request_id=REQUEST_A, last_attempt_at="2026-01-01T00:00:00Z")
        self.install_selection_race("rename")
        self.assertEqual(
            self.request(),
            (
                202,
                {
                    "state": "already_running",
                    "retry_after": 0,
                    "request_id": REQUEST_B,
                    "last_attempt_at": "2026-01-01T01:00:00.123456Z",
                },
            ),
        )
        self.assertEqual(json.loads((self.public / "selection-probe.json").read_text()), {"selections": 2})
        self.assertEqual({path.name for path in self.private.iterdir()}, {"request.running"})
        self.assertEqual(claimed.read_text(), REQUEST_B)
        self.assertEqual((claimed.stat().st_ino, claimed.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual(claimed.stat().st_mode & 0o777, 0o600)

    def test_re_resolution_is_bounded_when_both_selected_names_vanish(self):
        flag = self.private / "request.flag"
        flag.write_text(REQUEST_B)
        flag.chmod(0o600)
        self.status()
        self.install_selection_race("vanish-twice")
        self.assertEqual(self.request(), (503, {"state": "unavailable", "retry_after": 0}))
        self.assertEqual(json.loads((self.public / "selection-probe.json").read_text()), {"selections": 2})
        self.assertEqual(list(self.private.iterdir()), [])

    def test_claim_completed_during_re_resolution_preserves_the_fresh_cooldown(self):
        flag = self.private / "request.flag"
        flag.write_text(REQUEST_B)
        flag.chmod(0o600)
        self.status()
        self.install_selection_race("completed")
        code, body = self.request()
        self.assertEqual((code, body["state"]), (429, "rate_limited"))
        self.assertGreaterEqual(body["retry_after"], 295)
        self.assertLessEqual(body["retry_after"], 300)
        self.assertEqual(list(self.private.iterdir()), [])

    def test_selected_marker_security_failures_do_not_fall_back_to_another_claim(self):
        for mode in ("permissions", "symlink", "owner", "read-denied"):
            with self.subTest(mode=mode):
                flag, claimed = self.private / "request.flag", self.private / "request.running"
                for path, request_id in ((flag, REQUEST_B), (claimed, REQUEST_A)):
                    path.write_text(request_id)
                    path.chmod(0o600)
                self.status(result="running", request_state="running", request_id=REQUEST_A)
                self.install_selection_race(mode)
                self.assertEqual(self.request(), (503, {"state": "unavailable", "retry_after": 0}))
                self.assertEqual(json.loads((self.public / "selection-probe.json").read_text()), {"selections": 1})
                self.assertEqual(claimed.read_text(), REQUEST_A)
                self.assertEqual({path.name for path in self.private.iterdir()}, {"request.flag", "request.running"})
                flag.unlink()
                claimed.unlink()

    def test_legacy_and_malformed_flags_remain_untracked_and_unchanged(self):
        flag = self.private / "request.flag"
        for payload in (b"", b"malformed", b"x" * 100):
            with self.subTest(size=len(payload)):
                flag.write_bytes(payload)
                flag.chmod(0o600)
                code, body = self.request()
                self.assertEqual((code, body["state"], body["request_id"]), (202, "pending", None))
                self.assertEqual(flag.read_bytes(), payload)
                flag.unlink()

    def test_attempt_overtaking_status_read_before_publication_cannot_claim_new_id(self):
        self.status(result="success", request_id=REQUEST_A, last_attempt_at="2026-01-01T00:00:00Z")
        self.install_link_race("before-publication")
        code, body = self.request()
        self.assertEqual((code, body["state"]), (202, "pending"))
        self.assertRegex(body["request_id"], r"^[a-f0-9]{32}$")
        status = json.loads((self.public / "status.json").read_text())
        self.assertEqual(status["last_attempt_at"], "2026-01-01T01:00:00Z")
        self.assertIsNone(status["request_id"])
        self.assertEqual(body["last_attempt_at"], status["last_attempt_at"])
        self.assertEqual((self.private / "request.flag").read_text(), body["request_id"])
        self.assertEqual({path.name for path in self.private.iterdir()}, {"request.flag"})

    def test_fast_claim_and_completion_before_ack_retains_the_new_request_id(self):
        self.status(result="success", request_id=REQUEST_A, last_attempt_at="2026-01-01T00:00:00Z")
        self.install_link_race("after-publication")
        code, body = self.request()
        self.assertEqual((code, body["state"]), (202, "pending"))
        status = json.loads((self.public / "status.json").read_text())
        probe = json.loads((self.public / "race-probe.json").read_text())
        self.assertEqual(status["result"], "success")
        self.assertEqual(body["last_attempt_at"], status["last_attempt_at"])
        self.assertEqual(body["request_id"], status["request_id"])
        self.assertEqual(probe, {"id": body["request_id"], "mode": 0o600})
        self.assertEqual(list(self.private.iterdir()), [])

    def test_failed_atomic_publication_removes_only_the_temporary_flag(self):
        self.status(result="success", last_attempt_at="2026-01-01T00:00:00Z")
        self.install_link_race("publication-failed")
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(list(self.private.iterdir()), [])
