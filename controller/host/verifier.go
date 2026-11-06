package host

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/joeseverino/severino-hq/controller/internal/atomicfile"
)

// The verifier a deploy checks the composition's signature with: cosign, at
// the release and digest mise.lock records, which Renovate moves. It lives
// beside the tree, not in it, because the tree is replaced from the image the
// verifier checks.
//go:generate yq --input-format toml --output-format yaml --split-exp "\"verifier.gen.go\"" --from-file verifier.yq ../../mise.lock

// InstallVerifier puts the pinned cosign at path, unless it is there already.
// The download is checked against the pinned digest before it is made
// executable, and replaces what was there only then.
func InstallVerifier(ctx context.Context, client *http.Client, from, path string) error {
	return installVerifier(ctx, client, from, path, CosignSHA256)
}

func installVerifier(ctx context.Context, client *http.Client, from, path, pinned string) error {
	if digest, err := fileDigest(path); err == nil && digest == pinned {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	staged, err := atomicfile.Create(path, 0o755)
	if err != nil {
		return err
	}
	defer staged.Discard()
	if err := download(ctx, client, from, staged.File); err != nil {
		return fmt.Errorf("could not download cosign %s: %w", CosignVersion, err)
	}
	if digest, err := fileDigest(staged.Temporary()); err != nil || digest != pinned {
		return fmt.Errorf("refusing cosign %s: its digest is not the pinned one", CosignVersion)
	}
	return staged.Replace()
}

// download writes the release to file, from its start on each attempt.
func download(ctx context.Context, client *http.Client, from string, file *os.File) error {
	var last error
	for attempt := range 3 {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt) * 2 * time.Second):
			}
		}
		if err := file.Truncate(0); err != nil {
			return err
		}
		if _, err := file.Seek(0, io.SeekStart); err != nil {
			return err
		}
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, from, http.NoBody)
		if err != nil {
			return err
		}
		response, err := client.Do(request)
		if err != nil {
			last = err
			continue
		}
		if response.StatusCode != http.StatusOK {
			response.Body.Close()
			last = fmt.Errorf("GitHub answered %d", response.StatusCode)
			continue
		}
		_, err = io.Copy(file, io.LimitReader(response.Body, 512<<20))
		response.Body.Close()
		if err == nil {
			return nil
		}
		last = err
	}
	return last
}

func fileDigest(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}
