<?php
// Demo module: exists only to give a release a real declarative-schema change, so the deployment
// takes the "setup:upgrade + maintenance mode" path (see docs/architecture.md). Safe to delete
// together with its line in app/magento/config/config.php.
use Magento\Framework\Component\ComponentRegistrar;

ComponentRegistrar::register(ComponentRegistrar::MODULE, 'Proto_DeployDemo', __DIR__);
