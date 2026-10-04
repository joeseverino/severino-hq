package providers

import (
	"context"
	"os"
	"path/filepath"
	"strings"
)

// signingKey is a connection's signing key, rendered beside the SSH identities
// under the connection's own name so it is reachable only through it.
func (r *Registry) signingKey(ref string, public bool) (string, error) {
	if ref == "" || strings.Contains(ref, "/") || strings.HasPrefix(ref, ".") {
		return "", &ProviderError{Message: "Invalid signing connection."}
	}
	if _, ok := r.Env.Prefixes()[ref]; !ok {
		return "", &ProviderError{Message: "No connection named " + pyRepr(ref) + " was supplied to the controller."}
	}
	sshDir, err := r.Env.Required("HQ_CONTROLLER", "SSH_DIR")
	if err != nil {
		return "", err
	}
	name := ref + ".key"
	if public {
		name += ".pub"
	}
	return filepath.Join(sshDir, name), nil
}

// Sign signs data with a connection's key (SHA-256; RSA PKCS #1 v1.5 or ECDSA).
// The caller receives the signature and never the key.
func (r *Registry) Sign(ctx context.Context, ref string, data []byte) ([]byte, error) {
	path, err := r.signingKey(ref, false)
	if err != nil {
		return nil, err
	}
	step := "sign for " + ref
	failed := func() ([]byte, error) {
		r.commands().record(step, ref, "error")
		return nil, &ProviderError{Message: step + " failed."}
	}
	pemData, err := os.ReadFile(path)
	if err != nil {
		return failed()
	}
	key, err := firstPrivateKey(pemData)
	if err != nil {
		return failed()
	}
	signature, err := signDigest(key, data)
	if err != nil {
		return failed()
	}
	return signature, nil
}

// SigningPublicKey is the public half a connection's key was rendered with.
func (r *Registry) SigningPublicKey(ref string) (string, error) {
	path, err := r.signingKey(ref, true)
	if err != nil {
		return "", err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "", &ProviderError{Message: "No signing key was rendered for " + pyRepr(ref) + "."}
	}
	return string(data), nil
}
