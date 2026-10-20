<?php
header('Content-Type: application/json');
echo json_encode([
    'app' => 'smoke-image',
    'php' => PHP_VERSION,
    'arch' => php_uname('m'),
    'revision' => trim((string) @file_get_contents(__DIR__ . '/revision.txt')),
    'env' => getenv('APP_ENV') ?: 'unknown',
]), "\n";
