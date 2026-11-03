// Sends `data` to the fixture server, where tests read it with `pages.wait_for_report`. Reports arrive in order.
let reportChain = Promise.resolve();
function report(data) {
  const body = JSON.stringify(data);
  reportChain = reportChain.then(() => fetch("/__report", { method: "POST", body, keepalive: true })).catch(() => {});
}
