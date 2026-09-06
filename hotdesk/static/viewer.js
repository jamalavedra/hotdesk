const name = location.pathname.split('/')[2];
const interactive = new URLSearchParams(location.search).get('view_only') === 'false';
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has('token')) {
  sessionStorage.setItem('hotdesk-token', fragment.get('token'));
  history.replaceState(null, '', location.pathname + location.search);
}
const token = sessionStorage.getItem('hotdesk-token') || '';
const status = document.querySelector('#status');
const returnControl = document.querySelector('#return-control');
async function post(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw Error(result.error || 'Request failed');
  return result;
}
returnControl.addEventListener('click', async () => {
  returnControl.disabled = true;
  try {
    await post(`/api/desktops/${name}/control`, {control: 'agent'});
    location.replace(`/viewer/${name}/?view_only=true`);
  } catch (error) {
    status.textContent = error.message;
    returnControl.disabled = false;
  }
});
try {
  if (token) await post('/api/session', {});
  const {default: RFB} = await import(`/viewer/${name}/core/rfb.js`);
  const response = await fetch(`/viewer/${name}/session?interactive=${interactive ? 1 : 0}`);
  const session = await response.json();
  if (!response.ok) throw Error(session.error || 'Viewer access refused');
  const rfb = new RFB(document.querySelector('#screen'),
    `ws://${location.host}/viewer/${name}/websockify?interactive=${interactive ? 1 : 0}&generation=${session.generation}`,
    {credentials: {password: session.password}});
  rfb.viewOnly = !interactive;
  rfb.scaleViewport = true;
  rfb.addEventListener('connect', () => {
    returnControl.hidden = !interactive || !token;
    status.textContent = interactive ? 'You have control. Return control when finished.' : `Observing. Run hotdesk open ${name} to take control.`;
  });
  rfb.addEventListener('disconnect', () => { status.textContent = `Disconnected. Run hotdesk open ${name} to reconnect.`; });
  rfb.addEventListener('securityfailure', () => { status.textContent = 'Viewer authentication failed. Check workspace health.'; });
} catch (error) { status.textContent = error.message; }
