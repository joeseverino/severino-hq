package edge

import (
	"archive/tar"
	"bytes"
	"crypto"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"io"
	"time"
)

// The two files a certificate bundle holds, as the edge serves them.
const (
	Fullchain  = "fullchain.pem"
	PrivateKey = "privkey.pem"
)

// Bundle is the tar the controller's deploy sends and an edge's snapshot
// answers: the chain and its key, nothing else.
func Bundle(fullchain, privateKey []byte) []byte {
	var buffer bytes.Buffer
	writeTar(&buffer, tarFile{Fullchain, fullchain}, tarFile{PrivateKey, privateKey})
	return buffer.Bytes()
}

// tarFile is one regular file of a tar.
type tarFile struct {
	name string
	data []byte
}

// writeTar writes files as regular files readable by their owner alone, with
// no owner or time of their own, so the bytes depend on the contents alone.
func writeTar(out io.Writer, files ...tarFile) {
	archive := tar.NewWriter(out)
	for _, file := range files {
		_ = archive.WriteHeader(&tar.Header{Name: file.name, Mode: 0o600, Size: int64(len(file.data)), Typeflag: tar.TypeReg, ModTime: time.Unix(0, 0), Format: tar.FormatUSTAR})
		_, _ = archive.Write(file.data)
	}
	_ = archive.Close()
}

// pemEncode is a DER certificate as one PEM block.
func pemEncode(der []byte) []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

// The ways a bundle is refused, each its own message.
var (
	ErrBundleInvalid    = errors.New("certificate snapshot was invalid")
	ErrBundleUnexpected = errors.New("certificate snapshot contained unexpected files")
	ErrBundleIncomplete = errors.New("certificate snapshot was incomplete")
)

// ReadBundle is a bundle's chain and key: exactly the two names, each once
// and a regular file, so a link or a third file is never read or installed.
func ReadBundle(payload []byte) (fullchain, privateKey []byte, err error) {
	reader := tar.NewReader(bytes.NewReader(payload))
	files := map[string][]byte{}
	entries := 0
	irregular := false
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, nil, ErrBundleInvalid
		}
		entries++
		if header.Name != Fullchain && header.Name != PrivateKey {
			return nil, nil, ErrBundleUnexpected
		}
		if _, seen := files[header.Name]; seen {
			return nil, nil, ErrBundleUnexpected
		}
		files[header.Name] = nil
		if header.Typeflag != tar.TypeReg {
			irregular = true
			continue
		}
		data, err := io.ReadAll(reader)
		if err != nil {
			return nil, nil, ErrBundleInvalid
		}
		files[header.Name] = data
	}
	switch {
	case entries == 0:
		return nil, nil, ErrBundleInvalid
	case entries != 2:
		return nil, nil, ErrBundleUnexpected
	case irregular:
		return nil, nil, ErrBundleIncomplete
	}
	return files[Fullchain], files[PrivateKey], nil
}

// FirstCertificate is the first certificate block in data, which is the leaf
// of a chain.
func FirstCertificate(data []byte) (*x509.Certificate, error) {
	for {
		block, rest := pem.Decode(data)
		if block == nil {
			return nil, errors.New("no certificate")
		}
		if block.Type == "CERTIFICATE" || block.Type == "X509 CERTIFICATE" {
			return x509.ParseCertificate(block.Bytes)
		}
		data = rest
	}
}

// FirstPrivateKey is the first private key block in data.
func FirstPrivateKey(data []byte) (crypto.Signer, error) {
	for {
		block, rest := pem.Decode(data)
		if block == nil {
			return nil, errors.New("no private key")
		}
		switch block.Type {
		case "PRIVATE KEY":
			key, err := x509.ParsePKCS8PrivateKey(block.Bytes)
			if err != nil {
				return nil, err
			}
			signer, ok := key.(crypto.Signer)
			if !ok {
				return nil, errors.New("unsupported key")
			}
			return signer, nil
		case "RSA PRIVATE KEY":
			return x509.ParsePKCS1PrivateKey(block.Bytes)
		case "EC PRIVATE KEY":
			return x509.ParseECPrivateKey(block.Bytes)
		case "ENCRYPTED PRIVATE KEY":
			return nil, errors.New("encrypted key")
		}
		data = rest
	}
}

// ErrKeyMismatch is a key that is not the certificate's.
var ErrKeyMismatch = errors.New("certificate and private key do not match")

// Pair is the leaf of fullchain once privateKey is proven to be its key. A
// failure to read either side wraps ErrCertificateUnreadable or
// ErrKeyUnreadable.
func Pair(fullchain, privateKey []byte) (*x509.Certificate, error) {
	leaf, err := FirstCertificate(fullchain)
	if err != nil {
		return nil, errors.Join(ErrCertificateUnreadable, err)
	}
	key, err := FirstPrivateKey(privateKey)
	if err != nil {
		return nil, errors.Join(ErrKeyUnreadable, err)
	}
	certificatePublic, err := x509.MarshalPKIXPublicKey(leaf.PublicKey)
	if err != nil {
		return nil, errors.Join(ErrCertificateUnreadable, err)
	}
	keyPublic, err := x509.MarshalPKIXPublicKey(key.Public())
	if err != nil {
		return nil, errors.Join(ErrKeyUnreadable, err)
	}
	if !bytes.Equal(certificatePublic, keyPublic) {
		return nil, ErrKeyMismatch
	}
	return leaf, nil
}

// What Pair could not read.
var (
	ErrCertificateUnreadable = errors.New("certificate unreadable")
	ErrKeyUnreadable         = errors.New("private key unreadable")
)
