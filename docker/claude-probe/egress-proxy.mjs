import http from 'node:http';
import net from 'node:net';
import { pathToFileURL } from 'node:url';

const hosts = new Set([
  'api.anthropic.com',
  'claude.ai',
  'claude.com',
  'platform.claude.com',
  'sso.claude.com',
  'mcp-proxy.anthropic.com',
]);

export function allowedTarget(authority) {
  if (typeof authority !== 'string' || !/^[a-zA-Z0-9.-]+:443$/.test(authority)) return null;
  const target = new URL(`http://${authority}`);
  const host = target.hostname.toLowerCase().replace(/\.$/, '');
  return hosts.has(host) ? { host, port: 443 } : null;
}

export function createProxy(connect = net.connect) {
  const server = http.createServer((req, res) => {
    res.writeHead(403, { Connection: 'close' });
    res.end();
  });
  server.maxConnections = 64;
  server.on('connect', (req, client, head) => {
    const target = allowedTarget(req.url);
    if (!target) {
      client.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n');
      return;
    }
    let connected = false;
    const upstream = connect(target);
    client.on('error', () => upstream.destroy());
    upstream.on('error', () => {
      if (connected) client.destroy();
      else client.end('HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n');
    });
    upstream.setTimeout(120_000, () => upstream.destroy());
    client.setTimeout(120_000, () => client.destroy());
    upstream.once('connect', () => {
      connected = true;
      client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if (head.length) upstream.write(head);
      client.pipe(upstream);
      upstream.pipe(client);
    });
    client.once('close', () => upstream.destroy());
    upstream.once('close', () => client.destroy());
  });
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  // No payload, URL, credential or source-content logging.
  createProxy().listen(8080, '0.0.0.0');
}
