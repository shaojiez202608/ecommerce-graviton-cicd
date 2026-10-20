<?php
// Scopes and themes merged into app/etc/config.php at build time so that
// setup:static-content:deploy can run without a database (Magento key point 1/2).
// Keep this file in Git; environment-specific values never go here.
return [
    'scopes' => [
        'websites' => [
            'admin' => ['website_id' => '0', 'code' => 'admin', 'name' => 'Admin', 'sort_order' => '0', 'default_group_id' => '0', 'is_default' => '0'],
            'base' => ['website_id' => '1', 'code' => 'base', 'name' => 'Main Website', 'sort_order' => '0', 'default_group_id' => '1', 'is_default' => '1'],
        ],
        'groups' => [
            0 => ['group_id' => '0', 'website_id' => '0', 'name' => 'Default', 'root_category_id' => '0', 'default_store_id' => '0', 'code' => 'default'],
            1 => ['group_id' => '1', 'website_id' => '1', 'name' => 'Main Website Store', 'root_category_id' => '2', 'default_store_id' => '1', 'code' => 'main_website_store'],
        ],
        'stores' => [
            'admin' => ['store_id' => '0', 'code' => 'admin', 'website_id' => '0', 'group_id' => '0', 'name' => 'Admin', 'sort_order' => '0', 'is_active' => '1'],
            'default' => ['store_id' => '1', 'code' => 'default', 'website_id' => '1', 'group_id' => '1', 'name' => 'Default Store View', 'sort_order' => '0', 'is_active' => '1'],
        ],
    ],
    'themes' => [
        'frontend/Magento/blank' => ['parent_id' => null, 'theme_path' => 'Magento/blank', 'theme_title' => 'Magento Blank', 'is_featured' => '0', 'area' => 'frontend', 'type' => '0', 'code' => 'Magento/blank'],
        'adminhtml/Magento/backend' => ['parent_id' => null, 'theme_path' => 'Magento/backend', 'theme_title' => 'Magento 2 backend', 'is_featured' => '0', 'area' => 'adminhtml', 'type' => '0', 'code' => 'Magento/backend'],
        'frontend/Magento/luma' => ['parent_id' => 'Magento/blank', 'theme_path' => 'Magento/luma', 'theme_title' => 'Magento Luma', 'is_featured' => '0', 'area' => 'frontend', 'type' => '0', 'code' => 'Magento/luma'],
    ],
    // Locale (i18n) settings must also be in config.php for DB-less static content deploy.
    'system' => [
        'websites' => ['base' => ['general' => ['locale' => ['code' => 'en_US']]]],
        'stores' => ['default' => ['general' => ['locale' => ['code' => 'en_US']], 'design' => ['theme' => ['theme_id' => 'frontend/Magento/luma']]]],
        'default' => [
            'general' => ['locale' => ['code' => 'en_US']],
            'design' => ['theme' => ['theme_id' => 'frontend/Magento/luma']],
            'dev' => ['static' => ['sign' => '1']],
        ],
    ],
];
