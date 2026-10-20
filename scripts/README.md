# scripts

`deploy-image.sh` and `upgrade-container.sh` are the deploy host's shims, named by its sudo rules; they stay until `hq-host bootstrap` is re-run.
`check-plugin.sh` and `plugin-typecheck.sh` are the extension check, which stays while severino-extensions calls it by path.
Everything else is in `tools/`.
