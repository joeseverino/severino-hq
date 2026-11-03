// Package sealing is the format of a value HQ's web seals to the controller:
// HPKE (RFC 9180) with ML-KEM-768+X25519, HKDF-SHA256 and AES-256-GCM, to a
// key whose private half only the controller holds. hq/platform/core/secrets.py
// is the sealing half and must agree with this file on every constant here.
//
// A sealed value is the text "hq2.<key id>.<ciphertext>": the key id is the
// first 8 hex digits of SHA-256 over the public key, so a rotation can keep
// old keys to open with, and the ciphertext is HPKE's encapsulated key and
// sealed plaintext in unpadded URL-safe base64. Neither HPKE API takes
// associated data, so the value is bound to where it belongs through HPKE's
// info, "hq-seal/v1|<resource key>|<slot>": a ciphertext copied to another
// resource or slot does not open.
package sealing

import (
	"crypto/hpke"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
)

const (
	// SeedSize is the private key's length: the seed the hybrid KEM expands.
	SeedSize = 32
	// PublicKeySize is the public key's length: ML-KEM-768's 1184 bytes, then
	// X25519's 32.
	PublicKeySize = 1216
	// Prefix starts every sealed value.
	Prefix = "hq2"
	// infoVersion starts every info, so a later binding is never confused
	// with this one.
	infoVersion = "hq-seal/v1"
)

// The suite's three parts, as the decision fixed them.
var (
	kem  = hpke.MLKEM768X25519()
	kdf  = hpke.HKDFSHA256()
	aead = hpke.AES256GCM()
)

// Key is one private key, with the id a sealed value names it by.
type Key struct {
	ID      string
	private hpke.PrivateKey
}

// NewKey is the key a seed expands to.
func NewKey(seed []byte) (Key, error) {
	if len(seed) != SeedSize {
		return Key{}, fmt.Errorf("a sealing seed is %d bytes, not %d", SeedSize, len(seed))
	}
	private, err := kem.NewPrivateKey(seed)
	if err != nil {
		return Key{}, err
	}
	return Key{ID: KeyID(private.PublicKey().Bytes()), private: private}, nil
}

// PublicKey is the half the web seals to.
func (k Key) PublicKey() []byte { return k.private.PublicKey().Bytes() }

// KeyID names a public key in a sealed value's header.
func KeyID(public []byte) string {
	sum := sha256.Sum256(public)
	return hex.EncodeToString(sum[:4])
}

// Info binds a sealed value to its resource and slot. Neither may be empty or
// hold the separator, so no two places share an info.
func Info(resourceKey, slot string) ([]byte, error) {
	for _, part := range []string{resourceKey, slot} {
		if part == "" || strings.Contains(part, "|") {
			return nil, fmt.Errorf("a sealed value's resource key and slot are each non-empty and without %q", "|")
		}
	}
	return []byte(infoVersion + "|" + resourceKey + "|" + slot), nil
}

// ErrUnopened is every failure to open a value that is well formed: a wrong
// key, resource or slot, or a changed ciphertext, which HPKE cannot tell
// apart and neither should a caller.
var ErrUnopened = errors.New("the sealed value does not open for this resource and slot")

// Header splits a sealed value into the id of the key it was sealed to and
// its ciphertext.
func Header(sealed string) (id string, ciphertext []byte, err error) {
	prefix, rest, ok := strings.Cut(sealed, ".")
	if !ok || prefix != Prefix {
		return "", nil, fmt.Errorf("a sealed value starts %q", Prefix+".")
	}
	id, body, ok := strings.Cut(rest, ".")
	if !ok || len(id) != 8 {
		return "", nil, errors.New("a sealed value names its key in 8 hex digits")
	}
	ciphertext, err = base64.RawURLEncoding.DecodeString(body)
	if err != nil {
		return "", nil, fmt.Errorf("a sealed value's ciphertext is unpadded URL-safe base64: %w", err)
	}
	return id, ciphertext, nil
}

// Open is the plaintext ciphertext seals for resourceKey and slot.
func (k Key) Open(ciphertext []byte, resourceKey, slot string) ([]byte, error) {
	info, err := Info(resourceKey, slot)
	if err != nil {
		return nil, err
	}
	plaintext, err := hpke.Open(k.private, kdf, aead, info, ciphertext)
	if err != nil {
		return nil, ErrUnopened
	}
	return plaintext, nil
}
