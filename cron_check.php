<?php
/**
 * UK LEGO Price Tracker — Zero-dependency PHP script for cPanel Cron Jobs.
 *
 * Usage (cPanel Cron Job, twice daily: 0 9,18 * * *):
 *   php /home/YOURUSER/lego-price-bot/cron_check.php
 *
 * Test alert:
 *   php /home/YOURUSER/lego-price-bot/cron_check.php --test
 */

declare(strict_types=1);

$BASE_DIR = __DIR__;

function loadEnvFile(string $path): array {
    $env = [];
    if (!file_exists($path)) {
        return $env;
    }
    foreach (file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
        $line = trim($line);
        if ($line === '' || str_starts_with($line, '#') || !str_contains($line, '=')) {
            continue;
        }
        [$k, $v] = explode('=', $line, 2);
        $env[trim($k)] = trim($v);
    }
    return $env;
}

function httpGet(string $url, int $timeout = 15): ?string {
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_TIMEOUT => $timeout,
        CURLOPT_HTTPHEADER => [
            'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept-Language: en-GB,en;q=0.9',
        ],
    ]);
    $body = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return ($body !== false && $code === 200) ? $body : null;
}

function postDiscordWebhook(string $webhookUrl, array $payload): void {
    $ch = curl_init($webhookUrl);
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 15,
        CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
        CURLOPT_POSTFIELDS => json_encode($payload, JSON_UNESCAPED_SLASHES),
    ]);
    $resp = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if ($code >= 400) {
        fwrite(STDERR, "Discord webhook error ($code): $resp\n");
    }
}

function estimateUkShipping(float $price, string $retailer, bool $hasPrime, bool $preferCC): array {
    $p = round($price, 2);
    return match ($retailer) {
        'amazon_uk' => $hasPrime
            ? [0.0, $p, 'Free (Amazon Prime UK)']
            : ($p >= 35.0 ? [0.0, $p, 'Free UK delivery (over £35)'] : [4.49, round($p + 4.49, 2), '£4.49 UK delivery']),
        'argos' => $preferCC
            ? [0.0, $p, 'Free (Argos Click & Collect)']
            : [3.95, round($p + 3.95, 2), '£3.95 UK delivery (or free Click & Collect)'],
        'lego_uk' => $p >= 50.0
            ? [0.0, $p, 'Free UK delivery (over £50)']
            : [3.95, round($p + 3.95, 2), '£3.95 UK delivery (free over £50)'],
        'smyths' => ($preferCC || $p >= 20.0)
            ? [0.0, $p, 'Free UK delivery (over £20)']
            : [4.99, round($p + 4.99, 2), '£4.99 UK delivery (free over £20)'],
        'john_lewis' => $p >= 50.0
            ? [0.0, $p, 'Free UK delivery (over £50)']
            : [4.50, round($p + 4.50, 2), '£4.50 UK delivery (free over £50)'],
        default => [0.0, $p, 'Shipping not estimated'],
    };
}

function retailerDisplayName(string $retailer): string {
    return match ($retailer) {
        'amazon_uk' => 'Amazon UK',
        'argos' => 'Argos',
        'lego_uk' => 'LEGO.com UK',
        'smyths' => 'Smyths Toys',
        'john_lewis' => 'John Lewis',
        default => 'UK Retailer',
    };
}

function parseJsonLdGbpPrice(string $html): ?array {
    if (!preg_match_all('/<script[^>]+type=["\']application\/ld\+json["\'][^>]*>(.*?)<\/script>/is', $html, $matches)) {
        return null;
    }
    foreach ($matches[1] as $raw) {
        $data = json_decode(trim($raw), true);
        if (!is_array($data)) {
            continue;
        }
        $nodes = isset($data['@graph']) && is_array($data['@graph']) ? $data['@graph'] : [$data];
        foreach ($nodes as $node) {
            if (!is_array($node)) {
                continue;
            }
            $offers = $node['offers'] ?? (($node['@type'] ?? '') === 'Offer' ? $node : null);
            if (is_array($offers) && isset($offers[0])) {
                $offers = $offers[0];
            }
            if (!is_array($offers)) {
                continue;
            }
            if (strtoupper((string)($offers['priceCurrency'] ?? 'GBP')) !== 'GBP') {
                continue;
            }
            $avail = strtolower((string)($offers['availability'] ?? ''));
            if (str_contains($avail, 'outofstock')) {
                return ['price' => null, 'in_stock' => false];
            }
            $price = (float)($offers['price'] ?? $offers['lowPrice'] ?? 0);
            if ($price > 0) {
                return ['price' => round($price, 2), 'in_stock' => true];
            }
        }
    }
    return null;
}

function extractSearchGbpPrice(string $html, string $setNumber): ?float {
    $text = strip_tags($html);
    $pos = strpos($text, $setNumber);
    if ($pos === false) {
        return null;
    }
    $snippet = substr($text, max(0, $pos - 100), 400);
    $lower = strtolower($snippet);
    foreach (['out of stock', 'currently unavailable', 'sold out', 'no longer available'] as $oos) {
        if (str_contains($lower, $oos)) {
            return null;
        }
    }
    if (preg_match('/£\s*(\d{1,4}(?:\.\d{2})?)/', $snippet, $m)) {
        return round((float)$m[1], 2);
    }
    return null;
}

function analyzePrice(float $rrp, float $itemPrice, float $shipping, array $history, bool $isRetiring): array {
    $total = round($itemPrice + $shipping, 2);
    $savings = round($rrp - $total, 2);
    $discPct = $rrp > 0 ? round(($savings / $rrp) * 100, 1) : 0.0;
    $itemDiscPct = $rrp > 0 ? round((($rrp - $itemPrice) / $rrp) * 100, 1) : 0.0;

    $histLow = count($history) > 0 ? round(min($history), 2) : null;
    $histAvg = count($history) > 0 ? round(array_sum($history) / count($history), 2) : null;
    $nearLow = $histLow !== null && $total <= $histLow * 1.03;

    if ($isRetiring) {
        if ($discPct >= 20.0 || $nearLow) {
            $verdict = 'BUY NOW (RETIRING)';
            $forecast = 'This set is retiring soon and at a strong discount. Unlikely to drop further and prices typically rise once stock sells out.';
        } else {
            $verdict = 'BUY SOON (RETIRING)';
            $forecast = 'Retiring soon—waiting risks it selling out and going up on the secondary market.';
        }
    } elseif ($discPct >= 30.0 || ($nearLow && $discPct >= 20.0)) {
        $verdict = 'BUY NOW';
        $forecast = 'Rare/all-time low UK price range (20-30%+ off after shipping). Retailers usually bounce back up after this level of discount.';
    } elseif ($discPct >= 20.0) {
        $verdict = 'GOOD DEAL';
        $forecast = 'Solid UK discount (20%+ off). Likely to go back up once the current retailer promo ends.';
    } else {
        $verdict = 'WAIT';
        $forecast = 'Mild reduction after UK shipping. UK retailers regularly hit 20-25% off, so this is likely to drop more in the future.';
    }

    return [
        'rrp' => $rrp,
        'item_price' => $itemPrice,
        'shipping' => $shipping,
        'total' => $total,
        'savings' => $savings,
        'discount_pct' => $discPct,
        'item_discount_pct' => $itemDiscPct,
        'hist_low' => $histLow,
        'hist_avg' => $histAvg,
        'verdict' => $verdict,
        'forecast' => $forecast,
    ];
}

// --- Main Execution ---
$env = loadEnvFile($BASE_DIR . '/.env');
$webhookUrl = $env['DISCORD_WEBHOOK_URL'] ?? '';
$defaultUserId = $env['DISCORD_USER_ID'] ?? '';
$hasPrime = strtolower($env['HAS_AMAZON_PRIME'] ?? 'false') === 'true';
$preferCC = strtolower($env['PREFER_CLICK_AND_COLLECT'] ?? 'false') === 'true';
$cooldownHours = max(1, (int)($env['ALERT_COOLDOWN_HOURS'] ?? 12));

if ($webhookUrl === '' || $defaultUserId === '') {
    fwrite(STDERR, "Missing DISCORD_WEBHOOK_URL or DISCORD_USER_ID in .env\n");
    exit(1);
}

$wishlistPath = $BASE_DIR . '/' . ($env['WISHLIST_PATH'] ?? 'wishlist.json');
$wishlist = json_decode((string)@file_get_contents($wishlistPath), true);
if (!is_array($wishlist) || count($wishlist) === 0) {
    echo "Wishlist is empty or missing.\n";
    exit(0);
}

$dataDir = $BASE_DIR . '/data';
if (!is_dir($dataDir)) {
    mkdir($dataDir, 0755, true);
}
$statePath = $dataDir . '/php_state.json';
$state = json_decode((string)@file_get_contents($statePath), true) ?: ['items' => [], 'last_weekly' => null];

$isTest = in_array('--test', $argv ?? [], true) || (($_GET['test'] ?? '') === '1');
$forceWeekly = in_array('--weekly', $argv ?? [], true);

if ($isTest) {
    $first = $wishlist[0];
    $setNum = (string)($first['set_number'] ?? '10316');
    $rrp = (float)($first['rrp_gbp'] ?? 429.99);
    $simPrice = round($rrp * 0.72, 2);
    [$ship, $total, $shipNote] = estimateUkShipping($simPrice, 'argos', $hasPrime, $preferCC);
    $a = analyzePrice($rrp, $simPrice, $ship, [$rrp, round($rrp * 0.85, 2)], (bool)($first['is_retiring'] ?? false));
    $payload = [
        'content' => "<@{$defaultUserId}> LEGO sale spotted!",
        'embeds' => [[
            'title' => "[TEST] 🔥 NEW ALL-TIME LOW! LEGO Deal Alert: {$first['name']} (#{$setNum})",
            'description' => 'Best in-stock UK price spotted at **Argos**!',
            'color' => 0x2ECC71,
            'thumbnail' => ['url' => "https://images.brickset.com/sets/images/{$setNum}-1.jpg"],
            'fields' => [
                ['name' => 'Price Breakdown (GBP)', 'value' => "**Item:** £{$a['item_price']}\n**UK Shipping:** £{$a['shipping']} *({$shipNote})*\n**Total Cost:** **£{$a['total']}** (RRP £{$a['rrp']})", 'inline' => true],
                ['name' => 'Reduction', 'value' => "**Saved:** £{$a['savings']}\n**Total Discount:** {$a['discount_pct']}% off", 'inline' => true],
                ['name' => "Verdict: {$a['verdict']}", 'value' => $a['forecast'], 'inline' => false],
            ],
            'footer' => ['text' => 'PHP cPanel Cron Tracker • Prices in GBP (£)'],
        ]],
    ];
    postDiscordWebhook($webhookUrl, $payload);
    echo "Sent PHP test alert!\n";
    exit(0);
}

foreach ($wishlist as $entry) {
    $name = (string)$entry['name'];
    $setNum = (string)($entry['set_number'] ?? '');
    $key = $setNum !== '' ? $setNum : $name;
    $itemState = $state['items'][$key] ?? ['history' => [], 'last_alert_at' => 0, 'last_alert_price' => null];

    $rrp = (float)($entry['rrp_gbp'] ?? 0);
    $isRetiring = (bool)($entry['is_retiring'] ?? false);
    $minDisc = (float)($entry['min_discount_pct'] ?? 15.0);
    $target = isset($entry['target_price_gbp']) ? (float)$entry['target_price_gbp'] : null;

    // Auto-fetch RRP & retiring status from LEGO.com UK if missing
    $legoUrl = $setNum !== '' ? "https://www.lego.com/en-gb/product/{$setNum}" : null;
    $offers = [];

    if ($legoUrl !== null) {
        $legoHtml = httpGet($legoUrl);
        if ($legoHtml !== null) {
            if (str_contains(strtolower($legoHtml), 'retiring soon')) {
                $isRetiring = true;
            }
            $parsed = parseJsonLdGbpPrice($legoHtml);
            if ($parsed !== null && $parsed['price'] !== null) {
                if ($rrp <= 0) {
                    $rrp = $parsed['price'];
                }
                if ($parsed['in_stock']) {
                    [$ship, $tot, $sn] = estimateUkShipping($parsed['price'], 'lego_uk', $hasPrime, $preferCC);
                    $offers[] = ['retailer' => 'lego_uk', 'url' => $legoUrl, 'price' => $parsed['price'], 'ship' => $ship, 'total' => $tot, 'note' => $sn];
                }
            }
        }
    }

    $q = urlencode($setNum !== '' ? "LEGO {$setNum}" : $name);
    $searchTargets = [
        'argos' => "https://www.argos.co.uk/search/{$q}/",
        'smyths' => "https://www.smythstoys.com/uk/en-gb/search/?text={$q}",
        'john_lewis' => "https://www.johnlewis.com/search?search-term={$q}",
        'amazon_uk' => "https://www.amazon.co.uk/s?k={$q}",
    ];
    foreach ($searchTargets as $ret => $url) {
        $html = httpGet($url, 12);
        if ($html === null || $setNum === '') {
            continue;
        }
        $p = extractSearchGbpPrice($html, $setNum);
        if ($p !== null) {
            [$ship, $tot, $sn] = estimateUkShipping($p, $ret, $hasPrime, $preferCC);
            $offers[] = ['retailer' => $ret, 'url' => $url, 'price' => $p, 'ship' => $ship, 'total' => $tot, 'note' => $sn];
        }
    }

    if (count($offers) === 0) {
        echo "No in-stock UK offers found for {$name}\n";
        continue;
    }

    usort($offers, fn($a, $b) => $a['total'] <=> $b['total']);
    $best = $offers[0];
    if ($rrp <= 0) {
        $rrp = $best['price'];
    }

    $history = $itemState['history'];
    $prevLow = count($history) > 0 ? min($history) : null;
    $isAllTimeLow = $prevLow !== null && $best['total'] < $prevLow;

    $history[] = $best['total'];
    $itemState['history'] = array_slice($history, -90);
    $itemState['latest_total'] = $best['total'];
    $itemState['latest_retailer'] = retailerDisplayName($best['retailer']);

    $a = analyzePrice($rrp, $best['price'], $best['ship'], $history, $isRetiring);
    $hitTarget = $target !== null && $a['total'] <= $target;
    $hitDisc = $a['discount_pct'] >= $minDisc;
    $cooldownOk = (time() - (int)$itemState['last_alert_at']) >= ($cooldownHours * 3600)
        || ($itemState['last_alert_price'] !== null && $a['total'] <= $itemState['last_alert_price'] - 1.0);

    if (($hitTarget || $hitDisc) && $cooldownOk) {
        $notifyIds = $entry['notify_user_ids'] ?? [$defaultUserId];
        $pings = implode(' ', array_map(fn($id) => "<@{$id}>", $notifyIds));
        $atl = $isAllTimeLow ? '🔥 NEW ALL-TIME LOW! ' : '';
        $retName = retailerDisplayName($best['retailer']);
        $payload = [
            'content' => "{$pings} LEGO sale spotted!",
            'embeds' => [[
                'title' => "{$atl}LEGO Deal Alert: {$name}" . ($setNum ? " (#{$setNum})" : ''),
                'url' => $best['url'],
                'description' => "Best in-stock UK price spotted at **{$retName}**!",
                'color' => str_contains($a['verdict'], 'BUY NOW') ? 0x2ECC71 : 0xF1C40F,
                'thumbnail' => $setNum ? ['url' => "https://images.brickset.com/sets/images/{$setNum}-1.jpg"] : null,
                'fields' => [
                    ['name' => 'Price Breakdown (GBP)', 'value' => "**Item:** £{$a['item_price']}\n**UK Shipping:** £{$a['shipping']} *({$best['note']})*\n**Total Cost:** **£{$a['total']}** (RRP £{$a['rrp']})", 'inline' => true],
                    ['name' => 'Reduction', 'value' => "**Saved:** £{$a['savings']}\n**Total Discount:** {$a['discount_pct']}% off", 'inline' => true],
                    ['name' => "Verdict: {$a['verdict']}", 'value' => $a['forecast'], 'inline' => false],
                    ['name' => 'Link', 'value' => "[View on {$retName}]({$best['url']})", 'inline' => false],
                ],
                'footer' => ['text' => 'PHP cPanel Cron Tracker • Prices in GBP (£)'],
            ]],
        ];
        postDiscordWebhook($webhookUrl, $payload);
        $itemState['last_alert_at'] = time();
        $itemState['last_alert_price'] = $a['total'];
    }

    $state['items'][$key] = $itemState;
}

// Weekly Sunday Summary
$isSunday = (int)date('w') === 0;
$lastWeekly = (int)($state['last_weekly'] ?? 0);
if ($forceWeekly || ($isSunday && (time() - $lastWeekly) >= 6 * 86400)) {
    $lines = [];
    foreach ($wishlist as $entry) {
        $key = (string)($entry['set_number'] ?? $entry['name']);
        $st = $state['items'][$key] ?? null;
        if ($st && isset($st['latest_total'])) {
            $low = count($st['history']) > 0 ? min($st['history']) : $st['latest_total'];
            $lines[] = "• **{$entry['name']}** (#{$key}): **£{$st['latest_total']}** at {$st['latest_retailer']} (Low £{$low})";
        } else {
            $lines[] = "• **{$entry['name']}** (#{$key}): *No price recorded yet*";
        }
    }
    postDiscordWebhook($webhookUrl, [
        'content' => "<@{$defaultUserId}> Weekly UK LEGO price summary!",
        'embeds' => [[
            'title' => 'Weekly UK LEGO Wishlist Summary',
            'description' => implode("\n", $lines),
            'color' => 0x3498DB,
            'footer' => ['text' => 'cPanel PHP Cron Tracker • Checks run twice daily'],
        ]],
    ]);
    $state['last_weekly'] = time();
}

file_put_contents($statePath, json_encode($state, JSON_PRETTY_PRINT));
