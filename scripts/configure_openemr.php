<?php
// Only mount into the isolated demo. Called explicitly with `docker compose exec`.
// No web route is installed, and no password-grant shortcut is enabled.
$ignoreAuth = true; // CLI bootstrap only; this file is outside the web root.
$_SERVER['DOCUMENT_ROOT'] = '/var/www/localhost/htdocs/openemr';
$_SERVER['HTTP_HOST'] = 'localhost:9300';
$_SERVER['REQUEST_URI'] = '/';
$_GET['site'] = 'default';
require_once '/var/www/localhost/htdocs/openemr/interface/globals.php';
foreach ([
    'rest_api' => '1',
    'rest_fhir_api' => '1',
    'site_addr_oath' => 'https://localhost:9300',
] as $name => $value) {
    sqlStatement('DELETE FROM globals WHERE gl_name = ?', [$name]);
    sqlStatement('INSERT INTO globals (gl_name, gl_index, gl_value) VALUES (?, 0, ?)', [$name, $value]);
}
if (getenv('BRIDGE_DEMO_CLIENT_ID')) {
    sqlStatement('UPDATE oauth_clients SET is_enabled = 1 WHERE client_id = ?', [getenv('BRIDGE_DEMO_CLIENT_ID')]);
}
echo "Disposable demo APIs configured.\n";
