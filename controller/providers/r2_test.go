package providers

import (
	"cmp"
	"crypto/sha256"
	"encoding/hex"
	"encoding/pem"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/internal/secret"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

const (
	r2KeyID     = "example-access-key-id"
	r2SecretKey = "example-secret-access-key"
	r2Name      = "example-backups"
)

// lockedBucket is an R2 bucket under a retention lock, answering S3's
// ListObjectsV2 two keys a page and PutObject as R2 documents both: a write
// over an object refuses with 409, If-None-Match over one with 412.
type lockedBucket struct {
	mu      sync.Mutex
	objects map[string][]byte
	puts    int
	refuse  int
}

func (b *lockedBucket) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	b.mu.Lock()
	defer b.mu.Unlock()
	authorization := r.Header.Get("Authorization")
	if b.refuse != 0 || !strings.HasPrefix(authorization, "AWS4-HMAC-SHA256 Credential="+r2KeyID+"/") || !strings.Contains(authorization, "/auto/s3/aws4_request") {
		w.WriteHeader(cmp.Or(b.refuse, http.StatusForbidden))
		_, _ = io.WriteString(w, `<?xml version="1.0" encoding="UTF-8"?><Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>`)
		return
	}
	key, _ := strings.CutPrefix(r.URL.Path, "/"+r2Name)
	key = strings.TrimPrefix(key, "/")
	switch {
	case r.Method == http.MethodGet && key == "":
		b.list(w, r)
	case r.Method == http.MethodPut && key != "":
		body, _ := io.ReadAll(r.Body)
		digest := sha256.Sum256(body)
		if r.Header.Get("X-Amz-Content-Sha256") != hex.EncodeToString(digest[:]) {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = io.WriteString(w, `<Error><Code>XAmzContentSHA256Mismatch</Code><Message>The provided 'x-amz-content-sha256' header does not match what was computed.</Message></Error>`)
			return
		}
		if _, held := b.objects[key]; held {
			if r.Header.Get("If-None-Match") == "*" {
				w.WriteHeader(http.StatusPreconditionFailed)
				return
			}
			w.WriteHeader(http.StatusConflict)
			_, _ = io.WriteString(w, `<Error><Code>ObjectLockedByBucketPolicy</Code><Message>Object is locked by bucket policy.</Message></Error>`)
			return
		}
		b.objects[key] = body
		b.puts++
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func (b *lockedBucket) list(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	keys := []string{}
	for key := range b.objects {
		if strings.HasPrefix(key, query.Get("prefix")) {
			keys = append(keys, key)
		}
	}
	slices.Sort(keys)
	start, _ := strconv.Atoi(query.Get("continuation-token"))
	end := min(start+2, len(keys))
	if limit, _ := strconv.Atoi(query.Get("max-keys")); limit > 0 {
		end = min(start+limit, len(keys))
	}
	var page strings.Builder
	fmt.Fprintf(&page, `<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>%s</Name><KeyCount>%d</KeyCount><MaxKeys>1000</MaxKeys><IsTruncated>%t</IsTruncated>`,
		r2Name, end-start, end < len(keys))
	for _, key := range keys[start:end] {
		fmt.Fprintf(&page, `<Contents><Key>%s</Key><LastModified>2029-12-31T03:20:00.000Z</LastModified><ETag>"0"</ETag><Size>%d</Size><StorageClass>STANDARD</StorageClass></Contents>`, key, len(b.objects[key]))
	}
	if end < len(keys) {
		fmt.Fprintf(&page, `<NextContinuationToken>%d</NextContinuationToken>`, end)
	}
	page.WriteString(`</ListBucketResult>`)
	_, _ = io.WriteString(w, page.String())
}

// r2Fixture is a registry holding one R2 connection to a locked bucket, and
// the archive root lent the pass.
func r2Fixture(t *testing.T, bucket *lockedBucket, manages bool, lent string) *Registry {
	t.Helper()
	server := httptest.NewTLSServer(bucket)
	t.Cleanup(server.Close)
	caFile := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(caFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}), 0o600); err != nil {
		t.Fatal(err)
	}
	connection := connections.Connection{Ref: "r2", Provider: string(runtime.ConnectionProviderR2), Manages: manages,
		S3Bucket: &connections.S3Bucket{URL: server.URL, Bucket: r2Name, AccessKeyID: r2KeyID, SecretAccessKey: secret.New(r2SecretKey)}}
	return New(runtime.Environment{CAFile: caFile, Backup: lent}, supplied(connection), &fakeHTTP{})
}

func lentArchive(t *testing.T, name, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte(body), 0o640); err != nil {
		t.Fatal(err)
	}
	return path
}

const (
	r2Older  = "severino-hq-20291230T031500Z.tar.gz.age"
	r2Newest = "severino-hq-20291231T031500Z.tar.gz.age"
)

// A bucket that lacks the lent archive is given it, byte for byte, and the
// reading is the bucket's own listing after: its newest archive, across
// pages, and how many it keeps.
func TestTheLentArchiveIsCopiedAndTheBucketReadBack(t *testing.T) {
	bucket := &lockedBucket{objects: map[string][]byte{
		"severino-hq-20291228T031500Z.tar.gz.age": []byte("a"), "severino-hq-20291229T031500Z.tar.gz.age": []byte("b"),
		r2Older: []byte("c"), "unrelated.txt": []byte("not a backup"),
	}}
	r := r2Fixture(t, bucket, true, lentArchive(t, r2Newest, "ciphertext"))

	records, err := r.r2Backups(t.Context())
	if err != nil {
		t.Fatal(err)
	}

	if string(bucket.objects[r2Newest]) != "ciphertext" {
		t.Errorf("the bucket holds %q under the archive's name", bucket.objects[r2Newest])
	}
	want := runtime.R2BackupRecord{ConnectionRef: "r2", Bucket: r2Name, Key: r2Newest, SizeBytes: len("ciphertext"),
		UploadedAt: "2029-12-31T03:20:00.000Z", Copies: 4}
	if !slices.Equal(records, []any{want}) {
		t.Errorf("read %+v, want %+v", records, want)
	}
}

// A copy is written once: a bucket that holds the lent archive is not written
// to again, which its lock would refuse.
func TestABucketThatHoldsTheArchiveIsNotWritten(t *testing.T) {
	bucket := &lockedBucket{objects: map[string][]byte{r2Newest: []byte("ciphertext")}}
	r := r2Fixture(t, bucket, true, lentArchive(t, r2Newest, "ciphertext"))

	for range 2 {
		if _, err := r.r2Backups(t.Context()); err != nil {
			t.Fatal(err)
		}
	}
	if bucket.puts != 0 {
		t.Errorf("wrote %d times to a bucket that held the archive", bucket.puts)
	}
}

// HQ writes only through a connection that manages: one that does not still
// reads the bucket.
func TestAConnectionThatDoesNotManageOnlyReads(t *testing.T) {
	bucket := &lockedBucket{objects: map[string][]byte{r2Older: []byte("c")}}
	r := r2Fixture(t, bucket, false, lentArchive(t, r2Newest, "ciphertext"))

	records, err := r.r2Backups(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if bucket.puts != 0 || len(records) != 1 || records[0].(runtime.R2BackupRecord).Key != r2Older {
		t.Errorf("wrote %d, read %+v", bucket.puts, records)
	}
}

// A refusal fails the read in R2's own words, and names no secret.
func TestARefusedKeyFailsTheReadInR2sWords(t *testing.T) {
	bucket := &lockedBucket{objects: map[string][]byte{}, refuse: http.StatusForbidden}
	r := r2Fixture(t, bucket, true, lentArchive(t, r2Newest, "ciphertext"))

	_, err := r.r2Backups(t.Context())
	if err == nil || !strings.Contains(err.Error(), "AccessDenied") || strings.Contains(err.Error(), r2SecretKey) {
		t.Fatalf("read %v", err)
	}
	if failure, _, _ := runtime.Classify(err); failure != runtime.FailureClassPermission {
		t.Errorf("a 403 classed as %v", failure)
	}
}

// Only an archive is copied, whatever root lent.
func TestAFileThatIsNotAnArchiveIsNeverCopied(t *testing.T) {
	bucket := &lockedBucket{objects: map[string][]byte{}}
	r := r2Fixture(t, bucket, true, lentArchive(t, "severino-hq.sqlite3", "plaintext"))

	if _, err := r.r2Backups(t.Context()); err == nil || bucket.puts != 0 {
		t.Errorf("read %v after %d writes", err, bucket.puts)
	}
}
