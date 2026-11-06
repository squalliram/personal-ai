<?php
// Published into the Basic-Auth-protected dashboard. This endpoint only creates a private flag.
declare(strict_types=1);

ini_set('display_errors', '0');
const ENERGY_REFRESH_DIRECTORY_HEX = '__ENERGY_REFRESH_DIRECTORY_HEX__';
const ENERGY_REFRESH_COOLDOWN_SECONDS = 300;

function refresh_reply(int $code, string $state, int $retryAfter = 0, ?string $attemptAt = null, ?string $requestId = null): void
{
    http_response_code($code);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    if ($retryAfter > 0) {
        header('Retry-After: ' . $retryAfter);
    }
    $body = ['state' => $state, 'retry_after' => $retryAfter];
    if (in_array($state, ['pending', 'already_running', 'retry_queued'], true)) {
        $body['last_attempt_at'] = $attemptAt;
        $body['request_id'] = $requestId;
    }
    echo json_encode($body);
    exit;
}

function read_refresh_status(): array
{
    $path = __DIR__ . '/status.json';
    clearstatcache(true, $path);
    if (is_link($path)) {
        throw new RuntimeException('Refresh unavailable');
    }
    if (!is_file($path)) {
        return ['last_attempt_at' => null, 'request_id' => null];
    }
    if (filesize($path) > 65536) {
        throw new RuntimeException('Refresh unavailable');
    }
    $status = json_decode(file_get_contents($path), true);
    if (!is_array($status) || ($status['schema_version'] ?? null) !== 1
        || !array_key_exists('last_attempt_at', $status)
        || !in_array($status['request_state'] ?? null, [null, 'pending', 'running'], true)) {
        throw new RuntimeException('Refresh unavailable');
    }
    if (isset($status['last_attempt_at'])) {
        $stamp = $status['last_attempt_at'];
        if (!is_string($stamp) || !preg_match('/\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\z/', $stamp)) {
            throw new RuntimeException('Refresh unavailable');
        }
        $seconds = strtotime($stamp);
        if ($seconds === false || gmdate('Y-m-d\TH:i:s\Z', $seconds) !== substr($stamp, 0, 19) . 'Z') {
            throw new RuntimeException('Refresh unavailable');
        }
    }
    $id = $status['request_id'] ?? null;
    if ($id !== null && (!is_string($id) || !preg_match('/\A[a-f0-9]{32}\z/', $id))) {
        throw new RuntimeException('Refresh unavailable');
    }
    $status['request_id'] = $id;
    return $status;
}

class RefreshFlagVanished extends RuntimeException
{
}

function refresh_flag_vanished(string $path): bool
{
    clearstatcache();
    $directory = dirname($path);
    return !is_link($path) && !file_exists($path) && !is_link($directory)
        && realpath($directory) === $directory && is_dir($directory)
        && (fileperms($directory) & 0777) === 0700 && is_readable($directory) && is_executable($directory);
}

function read_refresh_id(string $path): ?string
{
    clearstatcache(true, $path);
    $metadata = @stat($path);
    if (is_link($path)) {
        throw new RuntimeException('Refresh unavailable');
    }
    if ($metadata === false) {
        if (refresh_flag_vanished($path)) {
            throw new RefreshFlagVanished('Refresh unavailable');
        }
        throw new RuntimeException('Refresh unavailable');
    }
    if (($metadata['mode'] & 0170000) !== 0100000 || ($metadata['mode'] & 0777) !== 0600
        || $metadata['uid'] !== fileowner(dirname($path))) {
        throw new RuntimeException('Refresh unavailable');
    }
    if ($metadata['size'] > 32) {
        return null;
    }
    $id = @file_get_contents($path, false, null, 0, 33);
    if ($id === false) {
        if (refresh_flag_vanished($path)) {
            throw new RefreshFlagVanished('Refresh unavailable');
        }
        throw new RuntimeException('Refresh unavailable');
    }
    // Legacy or malformed payloads are still served by the runner, but cannot identify a request.
    return is_string($id) && preg_match('/\A[a-f0-9]{32}\z/', $id) ? $id : null;
}

function reply_existing_refresh(array &$status, string $flag, string $running): bool
{
    // The runner may rename the selected flag while this request is reading it. Re-resolve once.
    for ($resolution = 0; $resolution < 2; $resolution++) {
        clearstatcache();
        $path = is_file($flag) ? $flag : (is_file($running) ? $running : null);
        $active = ($status['request_state'] ?? null) === 'running' || ($status['result'] ?? null) === 'running';
        if ($path !== null) {
            try {
                $id = read_refresh_id($path);
            } catch (RefreshFlagVanished $error) {
                if ($resolution === 1) {
                    throw $error;
                }
                // Retain the fresh cooldown watermark even if this claim has already completed.
                $status = read_refresh_status();
                continue;
            }
            $matches = $id === ($status['request_id'] ?? null);
            if ($path === $running && $active && $matches && isset($status['last_attempt_at'])) {
                refresh_reply(202, 'already_running', 0, $status['last_attempt_at'], $id);
            }
            if ($matches && ($status['request_state'] ?? null) === 'pending'
                && in_array($status['result'] ?? null, ['no_data', 'partial', 'failed'], true)
                && isset($status['last_attempt_at'])) {
                refresh_reply(202, 'retry_queued', 0, $status['last_attempt_at'], $id);
            }
            refresh_reply(202, 'pending', 0, $status['last_attempt_at'] ?? null, $id);
        }
        if ($active) {
            if (!isset($status['last_attempt_at'])) {
                throw new RuntimeException('Refresh unavailable');
            }
            refresh_reply(202, 'already_running', 0, $status['last_attempt_at'], $status['request_id'] ?? null);
        }
        return false;
    }
    throw new RuntimeException('Refresh unavailable');
}

function publish_refresh_flag(string $directory, string $flag, string $id): bool
{
    $temporary = $directory . '/.request-' . $id . '.tmp';
    $handle = @fopen($temporary, 'x');
    if ($handle === false) {
        throw new RuntimeException('Refresh unavailable');
    }
    try {
        if (fwrite($handle, $id) !== 32 || !fflush($handle)) {
            throw new RuntimeException('Refresh unavailable');
        }
        fclose($handle);
        $handle = null;
        // Exclusive publication exposes only a complete ID and never replaces another caller's request.
        return @link($temporary, $flag);
    } finally {
        if (is_resource($handle)) {
            fclose($handle);
        }
        unlink($temporary);
    }
}

set_error_handler(function (int $severity): bool {
    if (!(error_reporting() & $severity)) {
        return false;
    }
    throw new RuntimeException('Refresh unavailable');
});

try {
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
        header('Allow: POST');
        refresh_reply(405, 'method_not_allowed');
    }
    if (!in_array(strtolower((string) ($_SERVER['HTTPS'] ?? '')), ['on', '1'], true)) {
        refresh_reply(403, 'https_required');
    }
    // REMOTE_USER is set by the server after directory authentication; PHP_AUTH_USER alone is not proof.
    $user = $_SERVER['REMOTE_USER'] ?? $_SERVER['REDIRECT_REMOTE_USER'] ?? '';
    if (!is_string($user) || $user === '') {
        refresh_reply(503, 'auth_unavailable');
    }
    $host = strtolower((string) ($_SERVER['HTTP_HOST'] ?? ''));
    $origin = (string) ($_SERVER['HTTP_ORIGIN'] ?? '');
    if (!preg_match('/\A(?:[a-z0-9.-]+|\[[a-f0-9:]+\])(?::[0-9]{1,5})?\z/i', $host)
        || !hash_equals('https://' . $host, $origin)
        || !hash_equals('1', (string) ($_SERVER['HTTP_X_ENERGY_REFRESH'] ?? ''))
        || (isset($_SERVER['HTTP_SEC_FETCH_SITE']) && $_SERVER['HTTP_SEC_FETCH_SITE'] !== 'same-origin')) {
        refresh_reply(403, 'forbidden');
    }
    if (!ctype_xdigit(ENERGY_REFRESH_DIRECTORY_HEX) || strlen(ENERGY_REFRESH_DIRECTORY_HEX) % 2 !== 0) {
        refresh_reply(503, 'unavailable');
    }
    $directory = hex2bin(ENERGY_REFRESH_DIRECTORY_HEX);
    if ($directory === false || $directory === '' || $directory[0] !== '/'
        || is_link($directory) || realpath($directory) !== $directory
        || !is_dir($directory) || (fileperms($directory) & 0777) !== 0700) {
        refresh_reply(503, 'unavailable');
    }
    $flag = $directory . '/request.flag';
    $running = $directory . '/request.running';
    foreach ([$flag, $running] as $path) {
        if (is_link($path) || (file_exists($path) && (!is_file($path) || (fileperms($path) & 0777) !== 0600))) {
            refresh_reply(503, 'unavailable');
        }
    }
    $status = read_refresh_status();
    reply_existing_refresh($status, $flag, $running);
    $lastAttempt = isset($status['last_attempt_at']) ? strtotime($status['last_attempt_at']) : 0;
    $lastRequest = max($lastAttempt, is_file($flag) ? filemtime($flag) : 0, is_file($running) ? filemtime($running) : 0);
    $remaining = ENERGY_REFRESH_COOLDOWN_SECONDS - (time() - $lastRequest);
    if ($remaining > 0) {
        refresh_reply(429, 'rate_limited', min(ENERGY_REFRESH_COOLDOWN_SECONDS, $remaining));
    }
    umask(0077);
    $id = bin2hex(random_bytes(16));
    if (!publish_refresh_flag($directory, $flag, $id)) {
        $latest = read_refresh_status();
        reply_existing_refresh($latest, $flag, $running);
        refresh_reply(503, 'unavailable');
    }
    // A fast worker may already have claimed or fulfilled this ID. The watermark alone cannot prove that.
    $latest = read_refresh_status();
    refresh_reply(202, 'pending', 0, $latest['last_attempt_at'] ?? null, $id);
} catch (Throwable $error) {
    refresh_reply(503, 'unavailable');
}
