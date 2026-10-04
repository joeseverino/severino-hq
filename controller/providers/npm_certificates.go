package providers

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// npmCertificateName is the display name HQ gives the NPM certificate it installs for a consumer.
func npmCertificateName(consumer TLSConsumer) string {
	return "Severino HQ - " + consumer.Name
}

// jsonInt is an integer literal; a float, bool or string is not an id.
func jsonInt(raw json.RawMessage) (int, bool) {
	text := strings.TrimSpace(string(raw))
	if text == "" || strings.ContainsAny(text, ".eE") {
		return 0, false
	}
	value, err := strconv.Atoi(text)
	return value, err == nil
}

// idText is an id as Python's str() writes it into a path.
func idText(raw json.RawMessage) string {
	var text string
	if json.Unmarshal(raw, &text) == nil {
		return text
	}
	return strings.TrimSpace(string(raw))
}

// npmCertificateIDs is the NPM certificate id HQ installed for each NPM consumer, as last reported.
func npmCertificateIDsOf(spec TLSCertificateSpec, observed TLSCertificateObserved) npmCertificateIDs {
	consumers := []string{}
	for _, consumer := range spec.Consumers {
		if consumer.Kind == runtime.TLSConsumerKindNPM {
			consumers = append(consumers, consumer.Name)
		}
	}
	ids := map[string]int{}
	for name, raw := range observed.NPMCertificateIDs {
		if value, ok := jsonInt(raw); ok {
			ids[name] = value
		}
	}
	if single, ok := jsonInt(observed.NPMCertificateID); len(ids) == 0 && len(consumers) == 1 && ok {
		ids[consumers[0]] = single
	}
	known := npmCertificateIDs{}
	for _, name := range consumers {
		if value, ok := ids[name]; ok {
			known = append(known, npmCertificateID{name, value})
		}
	}
	return known
}

// withNPMCertificateIDs carries the installed ids into a report that did not install one.
func withNPMCertificateIDs(result Result, known npmCertificateIDs) Result {
	status, ok := result.Status.(*TLSCertificateStatus)
	if len(known) == 0 || !ok || status.NPMCertificateIDs != nil {
		return result
	}
	status.NPMCertificateIDs = known
	last := known[len(known)-1].ID
	status.NPMCertificateID = &last
	return result
}

// npmCertificateSummary is the part of an NPM certificate HQ reads.
type npmCertificateSummary struct {
	ID       json.RawMessage `json:"id"`
	NiceName json.RawMessage `json:"nice_name"`
	Provider json.RawMessage `json:"provider"`
}

func (c npmCertificateSummary) niceName() (string, bool) {
	var value string
	if json.Unmarshal(c.NiceName, &value) != nil {
		return "", false
	}
	return value, true
}

type npmCertificateCreate struct {
	Provider string `json:"provider"`
	NiceName string `json:"nice_name"`
}

type npmHostCertificate struct {
	CertificateID int `json:"certificate_id"`
}

func (r *Registry) npmCertificateList(ctx context.Context, base string, headers map[string]string) ([]npmCertificateSummary, error) {
	raw, err := r.HTTP.Request(ctx, base+"/nginx/certificates", "GET", headers, nil)
	if err != nil {
		return nil, err
	}
	list, err := decodeAs[[]npmCertificateSummary](raw, "Provider returned an invalid record list.")
	if list == nil && err == nil {
		list = []npmCertificateSummary{}
	}
	return list, err
}

// npmManagedCertificate uploads into the certificate HQ installed for this
// consumer, found by its recorded id, else by its display name, else created.
func (r *Registry) npmManagedCertificate(ctx context.Context, consumer TLSConsumer, certificateDomains []string, fullchain, privateKey []byte, knownID *int) (int, NPMCertificateIdentity, error) {
	base, headers, err := r.npmSession(ctx, "")
	if err != nil {
		return 0, NPMCertificateIdentity{}, err
	}
	niceName := npmCertificateName(consumer)
	certificates, err := r.npmCertificateList(ctx, base, headers)
	if err != nil {
		return 0, NPMCertificateIdentity{}, err
	}
	matches := []npmCertificateSummary{}
	if knownID != nil {
		for _, item := range certificates {
			if id, ok := jsonInt(item.ID); ok && id == *knownID {
				matches = append(matches, item)
			}
		}
	}
	if len(matches) == 0 {
		for _, item := range certificates {
			if name, ok := item.niceName(); ok && name == niceName {
				matches = append(matches, item)
			}
		}
	}
	if len(matches) > 1 {
		return 0, NPMCertificateIdentity{}, &ProviderError{Message: "NPM contains duplicate HQ-managed certificates."}
	}
	var idRaw json.RawMessage
	if len(matches) == 1 {
		var provider string
		if json.Unmarshal(matches[0].Provider, &provider) != nil || provider != "other" {
			return 0, NPMCertificateIdentity{}, &ProviderError{Message: "The HQ-managed NPM certificate is not a custom certificate."}
		}
		idRaw = matches[0].ID
	} else {
		created, err := r.HTTP.Request(ctx, base+"/nginx/certificates", "POST", headers, npmCertificateCreate{Provider: "other", NiceName: niceName})
		if err != nil {
			return 0, NPMCertificateIdentity{}, err
		}
		var answer struct {
			ID json.RawMessage `json:"id"`
		}
		if json.Unmarshal(created, &answer) == nil {
			idRaw = answer.ID
		}
	}
	certificateID, ok := jsonInt(idRaw)
	if !ok {
		return 0, NPMCertificateIdentity{}, &ProviderError{Message: "NPM did not return a managed certificate ID."}
	}
	leaf, chain, err := splitChain(fullchain)
	if err != nil {
		return 0, NPMCertificateIdentity{}, err
	}
	files := runtime.Multipart{
		{Field: "certificate", Filename: "certificate.pem", Content: leaf},
		{Field: "certificate_key", Filename: "certificate_key.pem", Content: privateKey},
		{Field: "intermediate_certificate", Filename: "intermediate_certificate.pem", Content: chain},
	}
	if _, err := r.HTTP.Request(ctx, base+"/nginx/certificates/validate", "POST", headers, files); err != nil {
		return 0, NPMCertificateIdentity{}, err
	}
	if _, err := r.HTTP.Request(ctx, base+"/nginx/certificates/"+strconv.Itoa(certificateID)+"/upload", "POST", headers, files); err != nil {
		return 0, NPMCertificateIdentity{}, err
	}
	hosts, err := r.npmCertificateHosts(ctx, base, headers)
	if err != nil {
		return 0, NPMCertificateIdentity{}, err
	}
	verify := nameSet(consumer.VerifyDomains)
	certificateNames := nameSet(certificateDomains)
	matching := []npmProxyHost{}
	for _, host := range hosts {
		selected, discovered := false, false
		for _, domain := range host.DomainNames {
			if verify[domain] {
				selected = true
			}
			if consumer.DiscoverCoveredHosts && certificateCovers(domain, certificateNames) {
				discovered = true
			}
		}
		if host.serving() && (selected || discovered) {
			matching = append(matching, host)
		}
	}
	covered := map[string]bool{}
	for _, host := range matching {
		for _, domain := range host.DomainNames {
			if verify[domain] {
				covered[domain] = true
			}
		}
	}
	missing := []string{}
	for _, domain := range sortedKeys(verify) {
		if !covered[domain] {
			missing = append(missing, domain)
		}
	}
	if len(missing) > 0 {
		return 0, NPMCertificateIdentity{}, &ProviderError{Message: "NPM has no proxy host for managed verification names: " + strings.Join(missing, ", ") + "."}
	}
	for _, host := range matching {
		// Uploading replaces NPM's files but does not reload nginx; re-applying each host does.
		if _, err := r.HTTP.Request(ctx, base+"/nginx/proxy-hosts/"+idText(host.ID), "PUT", headers, npmHostCertificate{CertificateID: certificateID}); err != nil {
			return 0, NPMCertificateIdentity{}, err
		}
	}
	return certificateID, NPMCertificateIdentity{NiceName: niceName}, nil
}
