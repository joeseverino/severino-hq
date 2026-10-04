package providers

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/npmapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// npmCertificateName is the display name HQ gives the NPM certificate it installs for a consumer.
func npmCertificateName(consumer TLSConsumer) string {
	return "Severino HQ - " + consumer.Name
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
	maps.Copy(ids, observed.NPMCertificateIDs)
	if single := observed.NPMCertificateID; len(ids) == 0 && len(consumers) == 1 && single != nil {
		ids[consumers[0]] = *single
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
	ID       int
	NiceName string
	Provider string
}

// npmCustomProvider is NPM's provider for a certificate uploaded rather than issued.
const npmCustomProvider = "other"

type npmCertificateCreate struct {
	Provider string `json:"provider"`
	NiceName string `json:"nice_name"`
}

type npmHostCertificate struct {
	CertificateID int `json:"certificate_id"`
}

// npmCertificateList reads NPM's certificates fresh, for the actions that write.
func (r *Registry) npmCertificateList(ctx context.Context, base string, headers map[string]string) ([]npmCertificateSummary, error) {
	raw, err := r.HTTP.Request(ctx, base+npmCertificatesSource.path, "GET", headers, nil)
	if err != nil {
		return nil, fmt.Errorf("npm %s: %w", npmCertificatesSource.what, err)
	}
	certificates, err := npmDecode[npmapi.CertificateObject](raw, "npm "+npmCertificatesSource.what)
	if err != nil {
		return nil, err
	}
	list := make([]npmCertificateSummary, 0, len(certificates))
	for _, certificate := range certificates {
		id, err := npmID(certificate.Id, "certificate")
		if err != nil {
			return nil, err
		}
		list = append(list, npmCertificateSummary{ID: id, NiceName: certificate.NiceName, Provider: certificate.Provider})
	}
	return list, nil
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
			if item.ID == *knownID {
				matches = append(matches, item)
			}
		}
	}
	if len(matches) == 0 {
		for _, item := range certificates {
			if item.NiceName == niceName {
				matches = append(matches, item)
			}
		}
	}
	if len(matches) > 1 {
		return 0, NPMCertificateIdentity{}, &ProviderError{Message: "npm holds more than one HQ-managed certificate for " + niceName}
	}
	var certificateID int
	if len(matches) == 1 {
		if matches[0].Provider != npmCustomProvider {
			return 0, NPMCertificateIdentity{}, &ProviderError{Message: "the HQ-managed npm certificate is not a custom certificate"}
		}
		certificateID = matches[0].ID
	} else {
		created, err := r.HTTP.Request(ctx, base+npmCertificatesSource.path, "POST", headers, npmCertificateCreate{Provider: npmCustomProvider, NiceName: niceName})
		if err != nil {
			return 0, NPMCertificateIdentity{}, fmt.Errorf("npm create certificate: %w", err)
		}
		var answer npmapi.CertificateObject
		if err := json.Unmarshal(created, &answer); err != nil {
			return 0, NPMCertificateIdentity{}, &ProviderError{Message: "npm create certificate answer did not decode", Err: err}
		}
		if certificateID, err = npmID(answer.Id, "created certificate"); err != nil {
			return 0, NPMCertificateIdentity{}, err
		}
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
	if _, err := r.HTTP.Request(ctx, base+npmCertificatesSource.path+"/validate", "POST", headers, files); err != nil {
		return 0, NPMCertificateIdentity{}, fmt.Errorf("npm validate certificate: %w", err)
	}
	if _, err := r.HTTP.Request(ctx, fmt.Sprintf("%s%s/%d/upload", base, npmCertificatesSource.path, certificateID), "POST", headers, files); err != nil {
		return 0, NPMCertificateIdentity{}, fmt.Errorf("npm upload certificate: %w", err)
	}
	hosts, err := r.npmProxyHostList(ctx, base, headers)
	if err != nil {
		return 0, NPMCertificateIdentity{}, err
	}
	verify := nameSet(consumer.VerifyDomains)
	certificateNames := nameSet(certificateDomains)
	matching := []npmapi.ProxyHostObject{}
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
		if bool(host.Enabled) && (selected || discovered) {
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
		return 0, NPMCertificateIdentity{}, &ProviderError{Message: "npm has no enabled proxy host for verification names " + strings.Join(missing, ", ")}
	}
	for _, host := range matching {
		id, err := npmID(host.Id, "proxy host")
		if err != nil {
			return 0, NPMCertificateIdentity{}, err
		}
		// Uploading replaces NPM's files but does not reload nginx; re-applying each host does.
		if _, err := r.HTTP.Request(ctx, fmt.Sprintf("%s%s/%d", base, npmProxyHosts.path, id), "PUT", headers, npmHostCertificate{CertificateID: certificateID}); err != nil {
			return 0, NPMCertificateIdentity{}, fmt.Errorf("npm attach certificate to proxy host %d: %w", id, err)
		}
	}
	return certificateID, NPMCertificateIdentity{NiceName: niceName}, nil
}
