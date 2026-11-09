import assert from 'node:assert/strict';
import net from 'node:net';
import { once } from 'node:events';
import test from 'node:test';
import { allowedTarget, createProxy } from './egress-proxy.mjs';

test('only exact approved hosts on TLS port 443 are admitted', () => {
  assert.deepEqual(allowedTarget('api.anthropic.com:443'), { host: 'api.anthropic.com', port: 443 });
  for (const value of [
    'api.anthropic.com.evil.test:443', 'evil.test:443', '127.0.0.1:443',
    'api.anthropic.com:80', 'user@api.anthropic.com:443',
    'api.anthropic.com:443/path', 'api.anthropic.com:443?data=secret',
  ]) assert.equal(allowedTarget(value), null);
});

async function listen(server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return server.address().port;
}

async function exchange(port, request) {
  return new Promise((resolve, reject) => {
    const socket = net.connect(port, '127.0.0.1');
    let response = '';
    socket.setTimeout(3000, () => socket.destroy(new Error('fixture timeout')));
    socket.on('error', reject);
    socket.on('data', data => { response += data.toString(); });
    socket.on('end', () => resolve(response));
    socket.on('connect', () => socket.write(request));
  });
}

test('denied CONNECT and plaintext HTTP never create an upstream socket', async () => {
  let calls = 0;
  const proxy = createProxy(() => { calls += 1; throw new Error('must not connect'); });
  const port = await listen(proxy);
  try {
    assert.match(await exchange(port, 'CONNECT evil.test:443 HTTP/1.1\r\nHost: evil.test\r\n\r\n'), /403/);
    assert.match(await exchange(port, 'GET http://api.anthropic.com/ HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n'), /403/);
    assert.equal(calls, 0);
  } finally { proxy.closeAllConnections(); proxy.close(); }
});

test('approved CONNECT tunnels bytes without inspecting TLS payloads', async () => {
  const upstream = net.createServer(socket => socket.once('data', data => socket.end(data)));
  const upstreamPort = await listen(upstream);
  const targets = [];
  const proxy = createProxy(target => {
    targets.push(target);
    return net.connect(upstreamPort, '127.0.0.1');
  });
  const port = await listen(proxy);
  try {
    const response = await exchange(port,
      'CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\nfixture-bytes');
    assert.match(response, /200 Connection Established/);
    assert.match(response, /fixture-bytes/);
    assert.deepEqual(targets, [{ host: 'api.anthropic.com', port: 443 }]);
  } finally { proxy.close(); upstream.close(); }
});
