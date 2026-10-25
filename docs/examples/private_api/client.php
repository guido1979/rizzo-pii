<?php
// Client di esempio della Rizzo PII Private API (PHP >= 8.1 con estensione curl).
//
//   RIZZO_PII_URL=http://rizzo-pii-api:5005 RIZZO_PII_API_KEY=... php client.php ingresso.pdf uscita.pdf

declare(strict_types=1);

const TIMEOUT_S = 330; // >= RIZZO_API_REQUEST_TIMEOUT

function rizzo_base(): string {
    return rtrim(getenv('RIZZO_PII_URL') ?: 'http://rizzo-pii-api:5005', '/');
}

function rizzo_call(string $path, array $curlOpts): array {
    $ch = curl_init(rizzo_base() . $path);
    $headers = $curlOpts[CURLOPT_HTTPHEADER] ?? [];
    $headers[] = 'Authorization: Bearer ' . getenv('RIZZO_PII_API_KEY'); // mai nel codice
    $respHeaders = [];
    // Niente CURLOPT_POST: CURLOPT_POSTFIELDS implica gia' POST, e un CURLOPT_POST
    // impostato DOPO un array di POSTFIELDS riporta curl a x-www-form-urlencoded (-> 415).
    curl_setopt_array($ch, $curlOpts + [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CONNECTTIMEOUT => 5,
        CURLOPT_TIMEOUT => TIMEOUT_S,
        CURLOPT_HEADERFUNCTION => function ($ch, $line) use (&$respHeaders) {
            $p = explode(':', $line, 2);
            if (count($p) === 2) { $respHeaders[strtolower(trim($p[0]))] = trim($p[1]); }
            return strlen($line);
        },
    ]);
    curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
    $body = curl_exec($ch);
    if ($body === false) { throw new RuntimeException('Rete: ' . curl_error($ch)); }
    $status = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    curl_close($ch);
    if ($status !== 200) {
        $err = json_decode($body, true)['error'] ?? ['code' => 'unknown', 'message' => '', 'request_id' => ''];
        throw new RuntimeException("{$status} {$err['code']}: {$err['message']} ({$err['request_id']})");
    }
    return [$body, $respHeaders];
}

/** PDF in memoria -> PDF anonimizzato in memoria (CURLStringFile: nessun file temporaneo). */
function rizzo_anonymize_pdf(string $pdfBytes, array $options): array {
    return rizzo_call('/api/v1/anonymize', [CURLOPT_POSTFIELDS => [
        'file' => new CURLStringFile($pdfBytes, 'documento.pdf', 'application/pdf'),
        'options' => json_encode($options),
    ]]);
}

function rizzo_anonymize_text(string $text, array $options = []): string {
    [$body] = rizzo_call('/api/v1/anonymize-text', [
        CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
        CURLOPT_POSTFIELDS => json_encode(['text' => $text, 'options' => (object) $options]),
    ]);
    return json_decode($body, true)['anonymized_text'];
}

[$pdf, $headers] = rizzo_anonymize_pdf(file_get_contents($argv[1]), [
    'preserve_letterhead' => true, 'letterhead_mode' => 'automatic', 'letterhead_pages' => 'all',
]);
file_put_contents($argv[2], $pdf);
print_r(array_filter($headers, fn ($k) => str_starts_with($k, 'x-pii-'), ARRAY_FILTER_USE_KEY));
echo rizzo_anonymize_text('Pratica di Giulietta Bramante, nata a Ravenna.'), PHP_EOL;
