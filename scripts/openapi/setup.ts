// Retries of one write retain this generated key; explicit caller headers win.
export default { config: { idempotencyKey: true } };
