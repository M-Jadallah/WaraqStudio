// This adapter is not attached to the live provider until the support gate
// passes. A permit lasts through the body, not merely through HTTP headers.
import { failure } from './rotation-policy.js';

export async function gatedRequest(coordinator, attemptId, send) {
  const permit = await coordinator.admit(attemptId, 'request');
  let finished = false;
  const finish = async uncertain => {
    if (finished) return;
    finished = true;
    await coordinator.complete(permit, { uncertain });
  };
  let response;
  try { response = await send(); }
  catch { await finish(true); throw failure('PROVIDER_TRANSPORT_UNCERTAIN'); }
  if (!(response instanceof Response)) { await finish(true); throw failure('INVALID_PROVIDER_RESPONSE'); }
  if (!response.body) { await finish(false); return response; }
  const reader = response.body.getReader();
  const sse = response.headers.get('content-type')?.includes('text/event-stream');
  const decoder = new TextDecoder();
  let buffer = '', terminal = false;
  const inspect = (chunk, final = false) => {
    buffer += decoder.decode(chunk, { stream: !final });
    const lines = buffer.split(/\r?\n/);
    buffer = final ? '' : lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data:')) continue;
      try {
        const event = JSON.parse(line.slice(5).trim());
        if (['response.completed', 'response.failed', 'response.incomplete'].includes(event.type)) terminal = true;
      } catch { /* Other SSE frames do not prove a terminal response. */ }
    }
    // A very long/unparseable SSE line cannot prove completion.
    if (buffer.length > 128_000) buffer = '';
  };
  const body = new ReadableStream({
    async pull(controller) {
      try {
        const { value, done } = await reader.read();
        if (done) {
          if (sse) inspect(undefined, true);
          await finish(Boolean(sse && !terminal));
          controller.close();
          reader.releaseLock();
        } else {
          if (sse) inspect(value);
          controller.enqueue(value);
        }
      } catch {
        try { await finish(true); } catch { /* Coordinator latches storage failures. */ }
        controller.error(failure('PROVIDER_STREAM_UNCERTAIN'));
      }
    },
    async cancel(reason) {
      try { await reader.cancel(reason); }
      finally { await finish(true); reader.releaseLock(); }
    },
  });
  return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
}
