package providers

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// The complete set of fields HQ writes about a certificate, with the type op
// stores each as. A declaration names where; this names what.
var publishedFields = []struct{ label, kind string }{
	{"Covers", "text"},
	{"Issued by", "text"},
	{"Expires", "date"},
	{"Fingerprint (SHA-256)", "text"},
	{"Installed on", "text"},
}

const (
	fingerprintLabel = "Fingerprint (SHA-256)"
	managedTag       = "hq-managed"
)

// storedAs is what op reports each assignment type as when it reads an item back.
var storedAs = map[string]string{"text": "STRING", "date": "DATE"}

// attachmentLabels are single words: op reads a dot in a label as a section separator.
var attachmentLabels = []string{"fullchain", "privkey"}

func publishedKind(label string) (string, bool) {
	for _, field := range publishedFields {
		if field.label == label {
			return field.kind, true
		}
	}
	return "", false
}

// certificateFingerprint is the one fingerprint HQ can say a certificate is,
// or none when consumers disagree.
func certificateFingerprint(status *TLSCertificateStatus) string {
	if status.ExpectedFingerprint != "" {
		return status.ExpectedFingerprint
	}
	seen := map[string]bool{}
	for _, item := range status.Consumers {
		seen[item.FingerprintSHA256] = true
	}
	if len(seen) == 1 {
		for fingerprint := range seen {
			return fingerprint
		}
	}
	return ""
}

// certificateFacts are the fields to write, all derived from HQ's own reading.
func certificateFacts(spec TLSCertificateSpec, status *TLSCertificateStatus) map[string]string {
	fingerprint := certificateFingerprint(status)
	if fingerprint == "" {
		return nil
	}
	installed := []string{}
	for _, consumer := range spec.Consumers {
		if consumer.Name != "" {
			installed = append(installed, consumer.Name)
		}
	}
	slices.Sort(installed)
	expires := status.NotAfter
	expires = runtime.ISODate(expires)
	return map[string]string{
		"Covers":                strings.Join(spec.Domains, ", "),
		"Issued by":             status.Issuer,
		"Expires":               expires,
		"Fingerprint (SHA-256)": fingerprint,
		"Installed on":          strings.Join(installed, ", "),
	}
}

// opField, opItem and opVault are the parts of `op --format json` read here.
type opField struct {
	Label string `json:"label"`
	Type  string `json:"type"`
	Value string `json:"value"`
}

type opItem struct {
	Fields []opField `json:"fields"`
	Tags   []string  `json:"tags"`
	Files  []struct {
		Name string `json:"name"`
	} `json:"files"`
}

type opVault struct {
	ID string `json:"id"`
}

type writtenField struct{ kind, value string }

// asWritten is one field's stored type beside its value in the form it was
// written in: op stores a date as epoch seconds at local midnight.
func asWritten(field opField) writtenField {
	kind, value := field.Type, field.Value
	if kind == "DATE" && value != "" {
		if seconds, err := strconv.ParseInt(strings.TrimSpace(value), 10, 64); err == nil {
			value = time.Unix(seconds, 0).Local().Format("2006-01-02")
		}
	}
	return writtenField{kind, value}
}

func (r *Registry) onePasswordToken(ref string) (string, error) {
	prefix, err := r.Env.Prefix(runtime.ConnectionProviderOnePassword, ref)
	if err != nil {
		return "", err
	}
	return r.Env.Required(prefix, "API_TOKEN")
}

func (r *Registry) onePasswordCurrent(ctx context.Context, publication OnePasswordPublication, token string) (map[string]writtenField, []string, map[string]bool, error) {
	raw, err := r.commands().Run(ctx,
		[]string{"op", "item", "get", publication.Item, "--vault", publication.Vault, "--format", "json"},
		nil, "1Password read for "+publication.Name, "", map[string]string{"OP_SERVICE_ACCOUNT_TOKEN": token})
	if err != nil {
		return nil, nil, nil, err
	}
	var item opItem
	if err := json.Unmarshal(raw, &item); err != nil {
		return nil, nil, nil, &ProviderError{Message: "1Password returned an unreadable item", Err: err}
	}
	fields := map[string]writtenField{}
	for _, field := range item.Fields {
		if _, owned := publishedKind(field.Label); owned {
			fields[field.Label] = asWritten(field)
		}
	}
	tags := []string{}
	for _, tag := range item.Tags {
		if strings.TrimSpace(tag) != "" {
			tags = append(tags, tag)
		}
	}
	files := map[string]bool{}
	for _, entry := range item.Files {
		if entry.Name != "" {
			files[entry.Name] = true
		}
	}
	return fields, tags, files, nil
}

// publishFacts writes HQ's facts onto one item, and only when they changed.
// It never deletes a field, the item, or a file, and never touches the note.
func (r *Registry) publishFacts(ctx context.Context, publication OnePasswordPublication, desired map[string]string, material func() ([]byte, []byte, error)) (PublishedFact, error) {
	// Both reach op's argv, where a leading dash is an option, not a name.
	if strings.HasPrefix(publication.Item, "-") || strings.HasPrefix(publication.Vault, "-") {
		return PublishedFact{}, &ProviderError{Message: "a 1Password item or vault name cannot start with a dash"}
	}
	token, err := r.onePasswordToken(publication.ConnectionRef)
	if err != nil {
		return PublishedFact{}, err
	}
	current, tags, files, err := r.onePasswordCurrent(ctx, publication, token)
	if err != nil {
		return PublishedFact{}, err
	}
	changed := []string{}
	for _, field := range publishedFields {
		if current[field.label] != (writtenField{storedAs[field.kind], desired[field.label]}) {
			changed = append(changed, field.label)
		}
	}
	slices.Sort(changed)
	tagged := false
	for _, tag := range tags {
		if tag == managedTag {
			tagged = true
		}
	}
	fingerprintMoved := false
	for _, label := range changed {
		if label == fingerprintLabel {
			fingerprintMoved = true
		}
	}
	missing := false
	for _, label := range attachmentLabels {
		if !files[label] {
			missing = true
		}
	}
	sendMaterial := material != nil && (fingerprintMoved || missing)
	yes := true
	if len(changed) == 0 && tagged && !sendMaterial {
		return PublishedFact{Target: publication.Name, Written: false, Fields: &[]string{}, Tagged: &yes, Material: "current"}, nil
	}
	assignments := []string{}
	for _, label := range changed {
		kind, _ := publishedKind(label)
		assignments = append(assignments, label+"["+kind+"]="+desired[label])
	}
	if sendMaterial {
		staged, err := os.MkdirTemp("", "hq-tls-")
		if err != nil {
			return PublishedFact{}, err
		}
		defer os.RemoveAll(staged)
		if err := os.Chmod(staged, 0o700); err != nil {
			return PublishedFact{}, err
		}
		fullchain, privateKey, err := material()
		if err != nil {
			return PublishedFact{}, err
		}
		for i, content := range [][]byte{fullchain, privateKey} {
			path := filepath.Join(staged, attachmentLabels[i])
			file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
			if err != nil {
				return PublishedFact{}, err
			}
			_, writeErr := file.Write(content)
			if closeErr := file.Close(); writeErr == nil {
				writeErr = closeErr
			}
			if writeErr != nil {
				return PublishedFact{}, writeErr
			}
			assignments = append(assignments, attachmentLabels[i]+"[file]="+path)
		}
	}
	union := map[string]bool{managedTag: true}
	for _, tag := range tags {
		union[tag] = true
	}
	argv := append([]string{"op", "item", "edit", publication.Item, "--vault", publication.Vault, "--tags", strings.Join(sortedKeys(union), ",")}, assignments...)
	if _, err := r.commands().Run(ctx, argv, nil, "1Password write for "+publication.Name, "", map[string]string{"OP_SERVICE_ACCOUNT_TOKEN": token}); err != nil {
		return PublishedFact{}, err
	}
	written := "current"
	if sendMaterial {
		written = "written"
	}
	return PublishedFact{Target: publication.Name, Written: true, Fields: &changed, Tagged: &yes, Material: written}, nil
}

// probeOnePassword proves the service account token is accepted, counting the vaults it reaches.
func (r *Registry) probeOnePassword(ctx context.Context, ref string) (ProbeResult, error) {
	token, err := r.onePasswordToken(ref)
	if err != nil {
		return ProbeResult{}, err
	}
	raw, err := r.commands().Run(ctx, []string{"op", "vault", "list", "--format", "json"},
		nil, "1Password preflight for "+ref, "", map[string]string{"OP_SERVICE_ACCOUNT_TOKEN": token})
	if err != nil {
		return ProbeResult{}, err
	}
	var vaults []opVault
	if err := json.Unmarshal(raw, &vaults); err != nil {
		return ProbeResult{}, &ProviderError{Message: "1Password returned an unreadable vault list", Err: err}
	}
	return ProbeResult{Detail: fmt.Sprintf("Service account accepted. It can access %d vaults.", len(vaults)), Reaches: []string{}}, nil
}
