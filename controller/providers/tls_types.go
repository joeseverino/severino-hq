package providers

import (
	"bytes"
	"encoding/json"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

const (
	npmCertificateIDsKey = "npm_certificate_ids"
)

// TLSConsumer is one place a certificate is served from.
type TLSConsumer struct {
	Kind                 runtime.TLSConsumerKind `json:"kind"`
	Name                 string                  `json:"name"`
	ConnectionRef        string                  `json:"connection_ref"`
	VerifyDomains        []string                `json:"verify_domains"`
	CertificateDirectory string                  `json:"certificate_directory,omitempty"`
	DiscoverCoveredHosts bool                    `json:"discover_covered_hosts,omitempty"`
	InstallDomains       []string                `json:"install_domains,omitempty"`
}

// OnePasswordPublication is where a certificate's facts are written, and nothing about what.
type OnePasswordPublication struct {
	Kind          string `json:"kind"`
	Name          string `json:"name"`
	ConnectionRef string `json:"connection_ref"`
	Vault         string `json:"vault"`
	Item          string `json:"item"`
}

// TLSCertificateSpec is the resolved spec of both certificate kinds.
type TLSCertificateSpec struct {
	CertificateName   string                   `json:"certificate_name"`
	Domains           []string                 `json:"domains"`
	Consumers         []TLSConsumer            `json:"consumers"`
	PublishTo         []OnePasswordPublication `json:"publish_to"`
	RenewalWindowDays int                      `json:"renewal_window_days"`
	Material          *runtime.Material        `json:"material,omitempty"`
}

// TLSCertificateObserved is what a certificate resource was last seen holding:
// the NPM certificate id installed for each NPM consumer (the single id is the
// older form, for a resource with one such consumer).
type TLSCertificateObserved struct {
	NPMCertificateIDs map[string]int `json:"npm_certificate_ids"`
	NPMCertificateID  *int           `json:"npm_certificate_id"`
}

// TLSObservation is what one consumer served for one name.
type TLSObservation struct {
	Domain            string                  `json:"domain"`
	NotAfter          string                  `json:"not_after"`
	FingerprintSHA256 string                  `json:"fingerprint_sha256"`
	Issuer            string                  `json:"issuer"`
	SANs              []string                `json:"sans"`
	Consumer          string                  `json:"consumer"`
	ConsumerKind      runtime.TLSConsumerKind `json:"consumer_kind"`
	MatchesExpected   *bool                   `json:"matches_expected,omitempty"`
	certificatePEM    string
}

// TLSUnreachable is a consumer that could not be read, and where the reading was tried.
type TLSUnreachable struct {
	Consumer string `json:"consumer"`
	Domain   string `json:"domain"`
	Endpoint string `json:"endpoint"`
	Port     string `json:"port"`
	Reason   string `json:"reason"`
}

// NPMCertificateIdentity names the NPM certificate HQ installed.
type NPMCertificateIdentity struct {
	NiceName string `json:"nice_name"`
}

// npmCertificateID is one NPM consumer's installed certificate id.
type npmCertificateID struct {
	Consumer string
	ID       int
}

// npmCertificateIDs keeps consumer order: the last one is the report's npm_certificate_id.
type npmCertificateIDs []npmCertificateID

func (ids npmCertificateIDs) MarshalJSON() ([]byte, error) {
	var out bytes.Buffer
	out.WriteByte('{')
	for i, item := range ids {
		if i > 0 {
			out.WriteByte(',')
		}
		key, _ := json.Marshal(item.Consumer)
		out.Write(key)
		out.WriteByte(':')
		value, _ := json.Marshal(item.ID)
		out.Write(value)
	}
	out.WriteByte('}')
	return out.Bytes(), nil
}

func (ids npmCertificateIDs) get(consumer string) (int, bool) {
	for _, item := range ids {
		if item.Consumer == consumer {
			return item.ID, true
		}
	}
	return 0, false
}

func (ids npmCertificateIDs) set(consumer string, id int) npmCertificateIDs {
	for i, item := range ids {
		if item.Consumer == consumer {
			ids[i].ID = id
			return ids
		}
	}
	return append(ids, npmCertificateID{consumer, id})
}

// cpanelSites keeps the plan's consumer order.
type cpanelSites struct {
	names []string
	sites map[string][]string
}

func (c *cpanelSites) MarshalJSON() ([]byte, error) {
	var out bytes.Buffer
	out.WriteByte('{')
	for i, name := range c.names {
		if i > 0 {
			out.WriteByte(',')
		}
		key, _ := json.Marshal(name)
		value, _ := json.Marshal(c.sites[name])
		out.Write(key)
		out.WriteByte(':')
		out.Write(value)
	}
	out.WriteByte('}')
	return out.Bytes(), nil
}

// TLSDeployment is what installing a certificate reports.
type TLSDeployment struct {
	NPMCertificateID       *int                    `json:"npm_certificate_id,omitempty"`
	NPMCertificateIdentity *NPMCertificateIdentity `json:"npm_certificate_identity,omitempty"`
	NPMCertificateIDs      npmCertificateIDs       `json:"npm_certificate_ids,omitempty"`
	CPanelSites            *cpanelSites            `json:"cpanel_sites,omitempty"`
}

// PublishedFact is the outcome of writing a certificate's facts to one item.
type PublishedFact struct {
	Target   string    `json:"target"`
	Written  bool      `json:"written"`
	Fields   *[]string `json:"fields,omitempty"`
	Tagged   *bool     `json:"tagged,omitempty"`
	Material string    `json:"material,omitempty"`
	Detail   string    `json:"detail,omitempty"`
}

// TLSCertificateStatus is a managed certificate's report.
type TLSCertificateStatus struct {
	Issuer               string           `json:"issuer"`
	NotAfter             string           `json:"not_after"`
	ArtifactNotAfter     string           `json:"artifact_not_after"`
	CertificatePEM       string           `json:"certificate_pem"`
	VerifiedDomains      []string         `json:"verified_domains"`
	Consumers            []TLSObservation `json:"consumers"`
	UnreachableConsumers []TLSUnreachable `json:"unreachable_consumers"`
	ExpectedFingerprint  string           `json:"expected_fingerprint_sha256,omitempty"`
	TLSDeployment
	ArtifactSource     string          `json:"artifact_source,omitempty"`
	RenewedFingerprint string          `json:"renewed_fingerprint_sha256,omitempty"`
	PublishedFacts     []PublishedFact `json:"published_facts,omitempty"`
}

// TLSVerificationEvidence is what each consumer served when a deployment did not activate.
type TLSVerificationEvidence struct {
	ExpectedFingerprint string                `json:"expected_fingerprint_sha256"`
	Consumers           []TLSConsumerEvidence `json:"consumers"`
}

type TLSConsumerEvidence struct {
	Consumer          string                  `json:"consumer"`
	Kind              runtime.TLSConsumerKind `json:"kind"`
	Domain            string                  `json:"domain"`
	FingerprintSHA256 string                  `json:"fingerprint_sha256"`
	MatchesExpected   bool                    `json:"matches_expected"`
}

// UploadedCertificateStatus is an uploaded certificate's report.
type UploadedCertificateStatus struct {
	CertificateName string   `json:"certificate_name"`
	Domains         []string `json:"domains"`
	TLSDeployment
}

// CertificateRemoval is the report of removing an uploaded certificate.
type CertificateRemoval struct {
	Removed bool `json:"removed"`
}
