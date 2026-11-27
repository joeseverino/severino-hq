package providers

import (
	"cmp"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	v4 "github.com/aws/aws-sdk-go-v2/aws/signer/v4"

	"github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// The off-site copy of HQ's backups: root lends an applying pass the host's
// newest archive, ciphertext alone, and this uploads it to each R2 bucket
// that lacks it, then reads the bucket back as the r2.backup reading. R2
// speaks S3's API; requests are signed with the SDK's SigV4 signer alone,
// which is a fraction of its S3 client's weight (docs/Backup.md).

const (
	// r2Region is the region every R2 bucket answers for.
	r2Region = "auto"
	// r2ListPages bounds one listing: a thousand keys a page, years of nights.
	r2ListPages = 100
	// r2PageBytes bounds one page of a listing.
	r2PageBytes = 8 << 20
	// r2ListBound and r2UploadBound bound one request of each.
	r2ListBound   = time.Minute
	r2UploadBound = 30 * time.Minute
)

// emptyPayload is the SHA-256 of a request with no body.
const emptyPayload = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

func (r *Registry) admitR2() {
	r.reader(runtime.ResourceKindR2Backup, r.r2Backups)
	r.probe(runtime.ConnectionProviderR2, r.r2Probe)
}

// r2Bucket is one connection's bucket, reached with its scoped key pair.
type r2Bucket struct {
	ref      string
	endpoint *url.URL
	bucket   string
	manages  bool
	key      aws.Credentials
	client   *http.Client
	now      func() time.Time
}

// r2Object is one archive a bucket's listing names.
type r2Object struct {
	Key          string `xml:"Key"`
	Size         int64  `xml:"Size"`
	LastModified string `xml:"LastModified"`
}

type r2Listing struct {
	Contents              []r2Object `xml:"Contents"`
	IsTruncated           bool       `xml:"IsTruncated"`
	NextContinuationToken string     `xml:"NextContinuationToken"`
}

// r2Refusal is the error document S3 answers a refused request with.
type r2Refusal struct {
	Code    string `xml:"Code"`
	Message string `xml:"Message"`
}

func (r *Registry) r2Bucket(ref string) (r2Bucket, error) {
	connection, err := r.Supplied.For(runtime.ConnectionProviderR2, ref)
	if err != nil {
		return r2Bucket{}, err
	}
	settings, err := runtime.Need(connection.S3Bucket)
	if err != nil {
		return r2Bucket{}, err
	}
	endpoint, err := url.Parse(strings.TrimRight(settings.URL, "/"))
	if err != nil || endpoint.Scheme != "https" || endpoint.User != nil || endpoint.Hostname() == "" || endpoint.Path != "" || endpoint.RawQuery != "" {
		return r2Bucket{}, &ProviderError{Message: "the R2 endpoint is not an https://<account>.r2.cloudflarestorage.com address", Failure: runtime.FailureClassAddress}
	}
	client, err := runtime.NewHTTPClient(r.Env.CAFile)
	if err != nil {
		return r2Bucket{}, err
	}
	return r2Bucket{
		ref: connection.Ref, endpoint: endpoint, bucket: settings.Bucket, manages: connection.Manages,
		key: aws.Credentials{AccessKeyID: settings.AccessKeyID, SecretAccessKey: settings.SecretAccessKey.Reveal()},
		// A signed request is never followed elsewhere: its signature names
		// this host.
		client: &http.Client{Transport: client.Transport, CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		}},
		now: r.Now,
	}, nil
}

// r2Backups uploads the archive root lent this pass to each bucket that lacks
// it, through a connection that manages, and reports each bucket's newest
// archive as its listing states it after.
func (r *Registry) r2Backups(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.refs(runtime.ConnectionProviderR2) {
		bucket, err := r.r2Bucket(ref)
		if err != nil {
			return nil, err
		}
		archives, err := bucket.list(ctx, 0)
		if err != nil {
			return nil, err
		}
		if lent := r.Env.Backup; lent != "" && bucket.manages && !slices.ContainsFunc(archives, func(o r2Object) bool { return o.Key == filepath.Base(lent) }) {
			if err := bucket.put(ctx, lent); err != nil {
				return nil, err
			}
			if archives, err = bucket.list(ctx, 0); err != nil {
				return nil, err
			}
		}
		if len(archives) == 0 {
			continue
		}
		// An archive's name carries when it was made, so the greatest is the newest.
		newest := slices.MaxFunc(archives, func(a, b r2Object) int { return cmp.Compare(a.Key, b.Key) })
		found = append(found, runtime.R2BackupRecord{
			ConnectionRef: bucket.ref, Bucket: bucket.bucket, Key: newest.Key, SizeBytes: int(newest.Size),
			UploadedAt: newest.LastModified, Copies: len(archives),
		})
	}
	return found, nil
}

// r2Probe lists one key, which the scoped pair may do and nothing outside its
// bucket can answer.
func (r *Registry) r2Probe(ctx context.Context, ref string) (ProbeResult, error) {
	bucket, err := r.r2Bucket(ref)
	if err != nil {
		return ProbeResult{}, err
	}
	if _, err := bucket.list(ctx, 1); err != nil {
		return ProbeResult{}, err
	}
	return ProbeResult{Detail: "Bucket " + bucket.bucket, Reaches: []string{}}, nil
}

// list is every archive in the bucket, or with limit, the first page of at
// most that many.
func (b r2Bucket) list(ctx context.Context, limit int) ([]r2Object, error) {
	archives := []r2Object{}
	token := ""
	for range r2ListPages {
		query := url.Values{"list-type": {"2"}, "prefix": {host.ArchivePrefix}}
		if limit > 0 {
			query.Set("max-keys", strconv.Itoa(limit))
		}
		if token != "" {
			query.Set("continuation-token", token)
		}
		page, err := b.listPage(ctx, query)
		if err != nil {
			return nil, err
		}
		for _, object := range page.Contents {
			if host.IsArchive(object.Key) {
				archives = append(archives, object)
			}
		}
		if limit > 0 || !page.IsTruncated || page.NextContinuationToken == "" {
			return archives, nil
		}
		token = page.NextContinuationToken
	}
	return nil, &ProviderError{Message: fmt.Sprintf("the bucket %s lists more than %d pages of archives", b.bucket, r2ListPages)}
}

func (b r2Bucket) listPage(ctx context.Context, query url.Values) (r2Listing, error) {
	ctx, cancel := context.WithTimeout(ctx, r2ListBound)
	defer cancel()
	address := b.endpoint.JoinPath(b.bucket)
	address.RawQuery = query.Encode()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, address.String(), http.NoBody)
	if err != nil {
		return r2Listing{}, err
	}
	response, err := b.send(request, emptyPayload)
	if err != nil {
		return r2Listing{}, err
	}
	defer response.Body.Close()
	var page r2Listing
	if err := xml.NewDecoder(io.LimitReader(response.Body, r2PageBytes)).Decode(&page); err != nil {
		return r2Listing{}, &ProviderError{Message: "the bucket's listing is not S3's ListObjectsV2 answer", Err: err}
	}
	return page, nil
}

// put uploads the archive at path under its own name. The request asks R2 to
// write only where no object is (If-None-Match), and the bucket's lock
// refuses an overwrite whatever it asks, so a copy is never replaced.
func (b r2Bucket) put(ctx context.Context, path string) error {
	name := filepath.Base(path)
	if !host.IsArchive(name) {
		return &ProviderError{Message: "the lent file is not a backup archive"}
	}
	file, err := os.Open(path)
	if err != nil {
		return &ProviderError{Message: "the lent archive cannot be read", Err: err}
	}
	defer file.Close()
	digest := sha256.New()
	size, err := io.Copy(digest, file)
	if err == nil {
		_, err = file.Seek(0, io.SeekStart)
	}
	if err != nil {
		return &ProviderError{Message: "the lent archive cannot be read", Err: err}
	}
	ctx, cancel := context.WithTimeout(ctx, r2UploadBound)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodPut, b.endpoint.JoinPath(b.bucket, name).String(), io.NopCloser(file))
	if err != nil {
		return err
	}
	request.ContentLength = size
	request.Header.Set("Content-Type", "application/octet-stream")
	request.Header.Set("If-None-Match", "*")
	response, err := b.send(request, hex.EncodeToString(digest.Sum(nil)))
	// A copy that arrived since the listing is the copy wanted.
	if refused, ok := errors.AsType[*ProviderError](err); ok && refused.HTTPStatus == http.StatusPreconditionFailed {
		return nil
	}
	if err != nil {
		return err
	}
	return response.Body.Close()
}

// send signs request for the bucket's key pair over payload's SHA-256 and
// answers its 2xx response; any other answer is its refusal, in S3's words.
func (b r2Bucket) send(request *http.Request, payload string) (*http.Response, error) {
	request.Header.Set("X-Amz-Content-Sha256", payload)
	// S3 signs the path as it is sent, not escaped again.
	if err := v4.NewSigner(func(o *v4.SignerOptions) { o.DisableURIPathEscaping = true }).
		SignHTTP(request.Context(), b.key, request, payload, "s3", r2Region, b.now()); err != nil {
		return nil, &ProviderError{Message: "the request to R2 could not be signed", Err: err}
	}
	response, err := b.client.Do(request) //nolint:gosec // G704: the address is the connection's own https endpoint, checked in r2Bucket, and no redirect is followed
	if err != nil {
		return nil, &ProviderError{Message: "R2 is unreachable", Failure: runtime.FailureClassNetwork, Err: err}
	}
	if response.StatusCode >= 200 && response.StatusCode < 300 {
		return response, nil
	}
	defer response.Body.Close()
	refusal := runtime.HTTPRefusal(response.StatusCode)
	var said r2Refusal
	if xml.NewDecoder(io.LimitReader(response.Body, 64<<10)).Decode(&said) == nil && said.Code != "" {
		refusal.Message = fmt.Sprintf("R2 answered %d %s: %s", response.StatusCode, said.Code, runtime.Clip(said.Message, runtime.ReasonLimit))
	}
	return nil, refusal
}
