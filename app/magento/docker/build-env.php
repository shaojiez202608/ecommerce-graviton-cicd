<?php
/**
 * Small helper for the immutable-image flow.
 *   merge-config <overlay.php> <config.php>  merge scopes/themes into config.php (build time)
 *   write-stub   <env.php> [nodb]            write a placeholder env.php for di:compile/SCD (build time)
 *   render       <env.php>                   write the real env.php from environment variables (run time)
 *   hold-lock    <name> <statusfile> <pid>   take MySQL GET_LOCK(name) and hold it while <pid> lives (run time)
 * Runtime values come from ECS task secrets (AWS Secrets Manager) and SSM parameters
 * injected as environment variables (Magento key point 2). No value is baked into the image.
 */
declare(strict_types=1);

function export_php(array $data): string
{
    return "<?php\nreturn " . var_export($data, true) . ";\n";
}

function env_required(string $name): string
{
    $value = getenv($name);
    if ($value === false || $value === '') {
        fwrite(STDERR, "missing required environment variable {$name}\n");
        exit(2);
    }
    return $value;
}

function env_or(string $name, string $default): string
{
    $value = getenv($name);
    return ($value === false || $value === '') ? $default : $value;
}

[$script, $command, $a, $b] = array_pad($argv, 4, null);

switch ($command) {
    case 'merge-config':
        $config = require $b;
        $overlay = require $a;
        file_put_contents($b, export_php(array_replace_recursive($config, $overlay)));
        break;

    case 'write-stub':
        // Build-time env.php. Magento only reads scopes/themes from config.php when the
        // deployment is neither "installed" (install date) nor has a database configured
        // (Magento\Store\App\Config\Source\InitialConfigSource). So:
        //   default -> placeholder db + install date, for deploy:mode:set and setup:di:compile
        //   nodb    -> neither, for setup:static-content:deploy
        $stub = [
            'MAGE_MODE' => 'production',
            'crypt' => ['key' => 'build-time-placeholder-not-used-at-runtime'],
            'backend' => ['frontName' => 'admin'],
        ];
        if ($b !== 'nodb') {
            $stub['install'] = ['date' => 'Thu, 01 Jan 2026 00:00:00 +0000'];
            $stub['db'] = ['table_prefix' => '', 'connection' => ['default' => [
                'host' => 'build-placeholder', 'dbname' => 'none', 'username' => 'none', 'password' => '',
                'model' => 'mysql4', 'engine' => 'innodb', 'initStatements' => 'SET NAMES utf8;', 'active' => '1',
            ]]];
            $stub['resource'] = ['default_setup' => ['connection' => 'default']];
        }
        file_put_contents($a, export_php($stub));
        break;

    case 'render':
        $redisHost = env_required('MAGENTO_REDIS_HOST');
        $redisScheme = env_or('MAGENTO_REDIS_TLS', '1') === '1' ? 'tls://' : '';
        $redisPort = env_or('MAGENTO_REDIS_PORT', '6379');
        $redisPassword = env_or('MAGENTO_REDIS_PASSWORD', '');
        $baseUrl = rtrim(env_required('MAGENTO_BASE_URL'), '/') . '/';
        // HTTPS environments: TLS ends at the ALB, Magento sees X-Forwarded-Proto (default offloader header).
        $secure = str_starts_with($baseUrl, 'https://') ? '1' : '0';
        $env = [
            'MAGE_MODE' => 'production',
            'backend' => ['frontName' => env_or('MAGENTO_ADMIN_FRONTNAME', 'admin')],
            'install' => ['date' => env_or('MAGENTO_INSTALL_DATE', 'Thu, 01 Jan 2026 00:00:00 +0000')],
            'crypt' => ['key' => env_required('MAGENTO_CRYPT_KEY')],
            'db' => ['table_prefix' => '', 'connection' => ['default' => [
                'host' => env_required('MAGENTO_DB_HOST'),
                'dbname' => env_required('MAGENTO_DB_NAME'),
                'username' => env_required('MAGENTO_DB_USER'),
                'password' => env_required('MAGENTO_DB_PASSWORD'),
                'model' => 'mysql4', 'engine' => 'innodb', 'initStatements' => 'SET NAMES utf8;', 'active' => '1',
                'driver_options' => [
                    // TLS to RDS using the CA bundle shipped in the image.
                    1009 => '/etc/ssl/certs/rds-global-bundle.pem', // PDO::MYSQL_ATTR_SSL_CA
                ],
            ]]],
            'resource' => ['default_setup' => ['connection' => 'default']],
            'session' => ['save' => 'redis', 'redis' => [
                'host' => $redisScheme . $redisHost, 'port' => $redisPort, 'database' => '2', 'password' => $redisPassword,
                'timeout' => '2.5', 'compression_library' => 'gzip', 'log_level' => '1',
                'max_concurrency' => '6', 'break_after_frontend' => '5', 'break_after_adminhtml' => '30',
                'first_lifetime' => '600', 'bot_first_lifetime' => '60', 'bot_lifetime' => '7200',
                'disable_locking' => '0', 'min_lifetime' => '60', 'max_lifetime' => '2592000',
            ]],
            'cache' => ['frontend' => [
                'default' => ['id_prefix' => env_or('MAGENTO_CACHE_PREFIX', 'm2_'), 'backend' => 'Magento\\Framework\\Cache\\Backend\\Redis',
                    'backend_options' => ['server' => $redisScheme . $redisHost, 'port' => $redisPort, 'database' => '0', 'password' => $redisPassword, 'compress_data' => '1']],
                'page_cache' => ['id_prefix' => env_or('MAGENTO_CACHE_PREFIX', 'm2_'), 'backend' => 'Magento\\Framework\\Cache\\Backend\\Redis',
                    'backend_options' => ['server' => $redisScheme . $redisHost, 'port' => $redisPort, 'database' => '1', 'password' => $redisPassword, 'compress_data' => '0']],
            ]],
            'cache_types' => array_fill_keys([
                'config', 'layout', 'block_html', 'collections', 'reflection', 'db_ddl', 'compiled_config',
                'eav', 'customer_notification', 'config_integration', 'config_integration_api', 'full_page',
                'config_webservice', 'translate',
            ], 1),
            'directories' => ['document_root_is_pub' => true],
            'system' => ['default' => [
                'web' => [
                    'unsecure' => ['base_url' => $baseUrl],
                    'secure' => ['base_url' => $baseUrl, 'use_in_frontend' => $secure, 'use_in_adminhtml' => $secure],
                ],
                'catalog' => ['search' => [
                    'engine' => 'opensearch',
                    'opensearch_server_hostname' => 'https://' . env_required('MAGENTO_OPENSEARCH_HOST'),
                    'opensearch_server_port' => '443',
                    'opensearch_index_prefix' => env_or('MAGENTO_OPENSEARCH_PREFIX', 'magento2'),
                    'opensearch_enable_auth' => '1',
                    'opensearch_username' => env_required('MAGENTO_OPENSEARCH_USER'),
                    'opensearch_password' => env_required('MAGENTO_OPENSEARCH_PASSWORD'),
                    'opensearch_server_timeout' => '15',
                ]],
            ]],
        ];
        file_put_contents($a, export_php($env));
        chmod($a, 0640);
        break;

    case 'hold-lock':
        // Session-scoped named lock: released automatically if this process or its connection dies.
        [$name, $status, $owner] = [$a, $b, (int) ($argv[4] ?? 0)];
        try {
            $pdo = new PDO('mysql:host=' . env_required('MAGENTO_DB_HOST'), env_required('MAGENTO_DB_MASTER_USER'),
                env_required('MAGENTO_DB_MASTER_PASSWORD'),
                [PDO::MYSQL_ATTR_SSL_CA => '/etc/ssl/certs/rds-global-bundle.pem', PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
            $got = (int) $pdo->query('SELECT GET_LOCK(' . $pdo->quote(substr($name, 0, 64)) . ', 0)')->fetchColumn();
        } catch (Throwable $t) {
            fwrite(STDERR, "lock: " . $t->getMessage() . "\n");
            $got = 0;
        }
        file_put_contents($status, $got === 1 ? 'ok' : 'busy');
        if ($got !== 1) {
            exit(3);
        }
        while ($owner > 0 && file_exists("/proc/{$owner}")) {
            sleep(2);
            $pdo->query('SELECT 1');  // keep the session (and the lock) alive
        }
        break;

    default:
        fwrite(STDERR, "usage: {$script} merge-config|write-stub|render|hold-lock ...\n");
        exit(64);
}
