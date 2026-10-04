package project

import (
	"bytes"
	"crypto/ed25519"
	"crypto/x509"
	"encoding/pem"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"golang.org/x/crypto/ssh"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/secrets/connectapi"
)

// KnownHosts is the name of the pinned host keys beside the identities.
const KnownHosts = "known_hosts"

var sshHost = regexp.MustCompile(`^[A-Za-z0-9.:-]+$`)
var digits = regexp.MustCompile(`^[0-9]+$`)

// keyField is an SSH Key item's private or public half, by the field's stable
// id or its label, as `op read op://vault/item/private key` resolved it.
func keyField(item connectapi.FullItem, id, label string) (string, bool) {
	found, value := 0, ""
	for _, field := range fields(item) {
		if field.Id == id || text(field.Label) == label {
			found++
			value = text(field.Value)
		}
	}
	return value, found == 1 && value != ""
}

// pair parses a key item's halves and proves they are one key: a mismatched
// item would ship a public key no private key here can use.
func pair(items []connectapi.FullItem, name string) (any, ssh.PublicKey, string) {
	matches := named(items, name)
	if len(matches) != 1 {
		return nil, nil, "names a key item the vault holds " + strconv.Itoa(len(matches)) + " of"
	}
	private, ok := keyField(matches[0], "private_key", "private key")
	if !ok {
		return nil, nil, "names an item with no single private key"
	}
	public, ok := keyField(matches[0], "public_key", "public key")
	if !ok {
		return nil, nil, "names an item with no single public key"
	}
	// The parser's own errors are not repeated: they describe key material.
	raw, err := ssh.ParseRawPrivateKey([]byte(private))
	if err != nil {
		return nil, nil, "names a private key that cannot be read"
	}
	if pointer, ok := raw.(*ed25519.PrivateKey); ok {
		raw = *pointer
	}
	signer, err := ssh.NewSignerFromKey(raw)
	if err != nil {
		return nil, nil, "names a private key of an unsupported type"
	}
	parsed, _, _, _, err := ssh.ParseAuthorizedKey([]byte(public))
	if err != nil {
		return nil, nil, "names a public key that cannot be read"
	}
	if !bytes.Equal(signer.PublicKey().Marshal(), parsed.Marshal()) {
		return nil, nil, "private and public halves do not match"
	}
	return raw, parsed, ""
}

// sameIdentity reports whether an installed OpenSSH private key is this one.
// The OpenSSH encoding holds random check bytes, so two encodings of one key
// differ; comparing bytes would rewrite every identity on every run.
func sameIdentity(public ssh.PublicKey) func([]byte) bool {
	return func(installed []byte) bool {
		block, _ := pem.Decode(installed)
		if block == nil || block.Type != "OPENSSH PRIVATE KEY" {
			return false
		}
		signer, err := ssh.ParsePrivateKey(installed)
		return err == nil && bytes.Equal(signer.PublicKey().Marshal(), public.Marshal())
	}
}

func keyName(value string) bool {
	return value != "" && !strings.Contains(value, "/") && !strings.HasPrefix(value, "op:")
}

// fileName reports whether a ref can name identity files without colliding
// with another file of the directory or leaving it.
func fileName(ref string) bool {
	return ref != "" && ref != KnownHosts && !strings.HasPrefix(ref, ".") && !strings.Contains(ref, "/") &&
		!strings.HasSuffix(ref, ".pub") && !strings.HasSuffix(ref, ".key")
}

// keys renders the SSH identity of every connection that opens a shell, with
// its host key pinned, and the signing key of every connection that signs.
func keys(input Input, document connections.Document) ([]File, int, int, error) {
	files := map[string]File{}
	var knownHosts bytes.Buffer
	identities, signing := 0, 0
	for _, connection := range document.Connections {
		ref, values := connection.Ref, connection.Values
		if identity, ok := values["IDENTITY"]; ok {
			if !fileName(ref) {
				return nil, 0, 0, refuse("Connection ", ref, " has a name its identity files would collide with.")
			}
			if !keyName(identity) {
				return nil, 0, 0, refuse("Connection ", ref, " names an invalid identity item.")
			}
			if !sshHost.MatchString(values["HOST"]) {
				return nil, 0, 0, refuse("Connection ", ref, " has an invalid host.")
			}
			port, err := strconv.Atoi(values["PORT"])
			if !digits.MatchString(values["PORT"]) || err != nil || port < 1 || port > 65535 {
				return nil, 0, 0, refuse("Connection ", ref, " has an invalid port.")
			}
			hostKey, _, _, rest, err := ssh.ParseAuthorizedKey([]byte(values["HOST_KEY"]))
			if !strings.HasPrefix(values["HOST_KEY"], ssh.KeyAlgoED25519+" ") || err != nil ||
				hostKey.Type() != ssh.KeyAlgoED25519 || len(bytes.TrimSpace(rest)) != 0 {
				return nil, 0, 0, refuse("Connection ", ref, " must pin an ssh-ed25519 host key.")
			}
			raw, public, problem := pair(input.Items, identity)
			if problem != "" {
				return nil, 0, 0, refuse("Connection ", ref, ": the identity ", problem, ".")
			}
			block, err := ssh.MarshalPrivateKey(raw, "")
			if err != nil {
				return nil, 0, 0, refuse("Connection ", ref, ": the identity cannot be written for ssh.")
			}
			files[ref] = File{Name: ref, Data: pem.EncodeToMemory(block), Mode: 0o400, Same: sameIdentity(public)}
			files[ref+".pub"] = File{Name: ref + ".pub", Data: ssh.MarshalAuthorizedKey(public), Mode: 0o444}
			knownHosts.WriteString("[" + values["HOST"] + "]:" + values["PORT"] + " " +
				strings.TrimSpace(string(ssh.MarshalAuthorizedKey(hostKey))) + "\n")
			identities++
		}
		if name, ok := values["SIGNING_KEY"]; ok {
			if !fileName(ref) {
				return nil, 0, 0, refuse("Connection ", ref, " has an invalid name.")
			}
			if !keyName(name) {
				return nil, 0, 0, refuse("Connection ", ref, " names an invalid signing key item.")
			}
			if _, both := files[ref]; both {
				return nil, 0, 0, refuse("Connection ", ref, " declares both an SSH identity and a signing key.")
			}
			raw, public, problem := pair(input.Items, name)
			if problem != "" {
				return nil, 0, 0, refuse("Connection ", ref, ": the signing key ", problem, ".")
			}
			// PKCS#8, for openssl, and read by nothing else.
			der, err := x509.MarshalPKCS8PrivateKey(raw)
			if err != nil {
				return nil, 0, 0, refuse("Connection ", ref, ": the signing key cannot be written for openssl.")
			}
			files[ref+".key"] = File{Name: ref + ".key", Data: pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), Mode: 0o400}
			files[ref+".key.pub"] = File{Name: ref + ".key.pub", Data: ssh.MarshalAuthorizedKey(public), Mode: 0o444}
			signing++
		}
	}
	files[KnownHosts] = File{Name: KnownHosts, Data: knownHosts.Bytes(), Mode: 0o444}
	names := make([]string, 0, len(files))
	for name := range files {
		names = append(names, name)
	}
	sort.Strings(names)
	sorted := make([]File, 0, len(names))
	for _, name := range names {
		sorted = append(sorted, files[name])
	}
	return sorted, identities, signing, nil
}
