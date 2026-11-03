# The sealing vector

A test key and a value HQ's Python sealed to it, shared by
`controller/runtime/seal_test.go` (the controller opens it) and
`hq/platform/core/tests/test_seal.py` (HQ seals to the same key, and the seed
opens it). Never a real key.

- `<key id>.seed`: 32 random bytes, named for its key id as hq-host names one.
- `public.key`: the seed's public key, as hq-host writes it.
- `vector.json`: the place a value was sealed for, its info, plaintext and
  sealed value.

To make a new one, write 32 random bytes to any `<name>.seed` here (removing
the old seed), then run `go -C controller test ./runtime -run
TestTheVectorsPublicKeyIsItsSeeds`: its failure prints the `public.key` to
write and the name the seed takes. Then seal the plaintext again from the
repository root, and put the printed value in `vector.json`'s `sealed`:

```sh
SEVERINO_SEAL_PUBLIC_KEY_FILE=controller/runtime/testdata/seal/public.key mise run manage -- shell -c "from hq.platform.core import secrets; print(secrets.seal_for_controller('an example secret', resource_key='example.service', slot='env:EXAMPLE_PASSWORD'))"
```
