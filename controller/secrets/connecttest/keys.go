package connecttest

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"strings"
	"testing"

	"golang.org/x/crypto/ssh"
)

// Key is a throwaway key pair as an SSH Key item holds it.
type Key struct {
	// PKCS8 is the private half as 1Password stores it; OpenSSH as ssh writes it.
	PKCS8, OpenSSH string
	// Public is the authorized-key line, without a comment.
	Public string
}

func encode(t testing.TB, private any) Key {
	t.Helper()
	der, err := x509.MarshalPKCS8PrivateKey(private)
	if err != nil {
		t.Fatal(err)
	}
	block, err := ssh.MarshalPrivateKey(private, "")
	if err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewSignerFromKey(private)
	if err != nil {
		t.Fatal(err)
	}
	return Key{
		PKCS8:   string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})),
		OpenSSH: string(pem.EncodeToMemory(block)),
		Public:  strings.TrimSpace(string(ssh.MarshalAuthorizedKey(signer.PublicKey()))),
	}
}

// Ed25519Key generates an identity key.
func Ed25519Key(t testing.TB) Key {
	t.Helper()
	_, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return encode(t, private)
}

// RSAKey generates a signing key.
func RSAKey(t testing.TB) Key {
	t.Helper()
	private, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return encode(t, private)
}

// KeyItem is an SSH Key item holding the pair, with the field ids and labels
// 1Password gives one.
func KeyItem(id, title, private, public string) Item {
	return Item{ID: id, Title: title, Fields: []Field{
		{ID: "private_key", Type: "SSHKEY", Label: "private key", Value: &private},
		{ID: "public_key", Type: "STRING", Label: "public key", Value: &public},
	}}
}

// HostKey generates an ssh-ed25519 public key line, as a connection pins one.
func HostKey() string {
	public, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		panic(err)
	}
	key, err := ssh.NewPublicKey(public)
	if err != nil {
		panic(err)
	}
	return strings.TrimSpace(string(ssh.MarshalAuthorizedKey(key)))
}
