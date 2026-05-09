# Wiring "Investigate with AI" into Zabbix UI

Zabbix can attach a custom right-click action to any problem via Frontend
Scripts. We use one of type **URL** that opens a signed link to the AI service.

## Requirement

- Token generation must happen server-side (signing key never goes to the
  browser). Two patterns work:
  1. (Recommended) Token signing endpoint on the AI service. Zabbix sends
     `eventid` and `instance` to a small signer route that returns the URL.
  2. (Lighter, used here) Generate the token in a tiny PHP wrapper colocated
     with Zabbix that has the signing key in its environment, then redirect.
     We do this in v0.4 because it requires no extra service call.

## Step-by-step

1. On the Zabbix server, create
   `/usr/share/zabbix/frontend-script-rca-ai.php`:

   ```php
   <?php
   $key = getenv('URL_SIGNING_KEY');
   if (!$key) { http_response_code(500); exit('signing key missing'); }
   $eventid = (int)($_GET['eventid'] ?? 0);
   $instance = preg_replace('/[^a-z0-9_-]/i', '', $_GET['instance'] ?? '');
   $payload = json_encode(['eventid' => $eventid, 'instance' => $instance],
                          JSON_UNESCAPED_SLASHES);
   $exp = time() + 300;
   $b64 = function ($s) {
       return rtrim(strtr(base64_encode($s), '+/', '-_'), '=');
   };
   $payload_p = $b64($payload);
   $exp_p = $b64((string)$exp);
   $sig = hash_hmac('sha256', "$payload_p.$exp_p", $key, true);
   $sig_p = $b64($sig);
   header('Location: https://zabbix-ai.internal/investigate?token=' .
          "$payload_p.$exp_p.$sig_p");
   ```

   `chmod 0640`, owner `www-data:www-data`. Make sure the Apache/nginx
   environment has `URL_SIGNING_KEY` set (matches the AI service's env).

2. In Zabbix UI: **Configure → Scripts → Create script**:
   - Name: `Investigate with AI`
   - Scope: `Manual event action`
   - Type: `URL`
   - URL:
     `/frontend-script-rca-ai.php?eventid={EVENT.ID}&instance=monitoring`
   - Permissions: limit to NOC user groups

3. Save. The action now appears on the right-click menu of any problem in
   the Problems view.

## Alternative without a PHP wrapper

If you don't want a PHP wrapper, expose `/sign?eventid=…&instance=…` on
the AI service behind IP-restricted auth and have a tiny shell script do
the curl. Same trust model — signing key still server-side.
