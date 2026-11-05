/* Cynea: tell the API about unhandled errors on this page.
 *
 * The API forwards them to the error tracker, if one is configured, and drops
 * them if not. Only the message and the page's path go: no URL query strings
 * (they can hold tickets and reset tokens), no form values.
 */
'use strict';

(function () {
  var sent = 0;
  var MAX_PER_PAGE = 5;      // one broken loop should not send thousands

  function api() {
    var override = null;
    try { override = localStorage.getItem('cynea_api'); } catch (e) {}
    if (override) return override.replace(/\/$/, '');
    var h = location.hostname;
    return (h === 'localhost' || h === '127.0.0.1') ? 'http://127.0.0.1:8000' : '/api';
  }

  function report(message) {
    if (sent >= MAX_PER_PAGE || !message) return;
    sent++;
    try {
      fetch(api() + '/client-error', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: String(message).slice(0, 1000), page: location.pathname }),
        keepalive: true,
      }).catch(function () {});
    } catch (e) {}
  }

  window.addEventListener('error', function (e) {
    report(e.message + (e.filename ? ' at ' + e.filename.split('?')[0] + ':' + e.lineno : ''));
  });
  window.addEventListener('unhandledrejection', function (e) {
    var r = e.reason;
    report('Unhandled rejection: ' + (r && r.message ? r.message : r));
  });
})();
