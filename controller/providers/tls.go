package providers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

func (r *Registry) admitTLS() {
	act(r, runtime.ResourceKindTLSCertificate, "reconcile", r.tlsReconcile, invalidSpec(certificateSpecInvalid))
	act(r, runtime.ResourceKindTLSCertificate, "renew", r.tlsRenew, invalidSpec(certificateSpecInvalid), ignoresSpecInPlan())
	act(r, runtime.ResourceKindTLSUploadedCertificate, "reconcile", r.uploadedReconcile, invalidSpec(certificateSpecInvalid))
	act(r, runtime.ResourceKindTLSUploadedCertificate, "delete", r.uploadedDelete, invalidSpec(certificateSpecInvalid))
	r.probe(runtime.ConnectionProviderOnePassword, r.probeOnePassword)
}

const certificateSpecInvalid = "certificate spec is invalid"

// NeedsMaterial says whether an action on kind needs HQ's stored certificate material.
func (r *Registry) NeedsMaterial(kind runtime.ResourceKind) bool {
	return kind == runtime.ResourceKindTLSUploadedCertificate
}

// cpanelSites is the cPanel sites a consumer installs on, decided before anything is
// issued: the sites serving its install names, else every site serving a checked name.
func (r *Registry) cpanelSitesFor(ctx context.Context, consumer TLSConsumer) ([]string, error) {
	raw, err := r.commands().SSH(ctx, consumer.ConnectionRef, "sites", nil)
	if err != nil {
		return nil, err
	}
	var answer struct {
		Sites map[string][]string `json:"sites"`
	}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &answer); err != nil {
			return nil, &ProviderError{Message: consumer.Name + " returned an unreadable site list", Err: err}
		}
	}
	if len(answer.Sites) == 0 {
		return nil, &ProviderError{Message: consumer.Name + " reported no sites"}
	}
	namesOf := map[string][]string{}
	siteOf := map[string]string{}
	for site, aliases := range answer.Sites {
		names := append([]string{site}, aliases...)
		namesOf[site] = names
		for _, name := range names {
			siteOf[strings.ToLower(name)] = site
		}
	}
	lowered := func(values []string) []string {
		set := map[string]bool{}
		for _, value := range values {
			set[strings.ToLower(value)] = true
		}
		return sortedKeys(set)
	}
	verify, declared := lowered(consumer.VerifyDomains), lowered(consumer.InstallDomains)
	notHosted := []string{}
	for _, name := range append(append([]string{}, verify...), declared...) {
		if _, ok := siteOf[name]; !ok {
			notHosted = append(notHosted, name)
		}
	}
	slices.Sort(notHosted)
	if len(notHosted) > 0 {
		return nil, &ProviderError{Message: consumer.Name + " does not serve " + strings.Join(notHosted, ", ") + "; remove the name from the target, or add it to the hosting account"}
	}
	wanted := declared
	if len(wanted) == 0 {
		wanted = verify
	}
	chosenSet := map[string]bool{}
	for _, name := range wanted {
		chosenSet[siteOf[name]] = true
	}
	chosen := sortedKeys(chosenSet)
	served := map[string]bool{}
	for _, site := range chosen {
		for _, name := range namesOf[site] {
			served[strings.ToLower(name)] = true
		}
	}
	unserved := []string{}
	for _, name := range verify {
		if !served[name] {
			unserved = append(unserved, name)
		}
	}
	if len(unserved) > 0 {
		return nil, &ProviderError{Message: consumer.Name + " would be checked at " + strings.Join(unserved, ", ") + " but installs only on " + strings.Join(chosen, ", ") + "; add those names to the target's install list, or leave it empty to install on every site that serves a checked name"}
	}
	if len(chosen) == 0 {
		return nil, &ProviderError{Message: consumer.Name + " has no site to install on"}
	}
	return chosen, nil
}

// deploymentPlan is everything a deploy needs from the consumers, asked before a
// certificate is requested or any consumer is touched.
type deploymentPlan map[string][]string

func (r *Registry) planDeployment(ctx context.Context, spec TLSCertificateSpec) (deploymentPlan, error) {
	plan := deploymentPlan{}
	for _, consumer := range spec.Consumers {
		if consumer.Kind == runtime.TLSConsumerKindCPanel {
			sites, err := r.cpanelSitesFor(ctx, consumer)
			if err != nil {
				return nil, err
			}
			plan[consumer.Name] = sites
		}
	}
	return plan, nil
}

// cpanelDeploy is the cPanel deploy command's input.
type cpanelDeploy struct {
	Sites    []string `json:"sites"`
	Cert     string   `json:"cert"`
	Key      string   `json:"key"`
	CABundle string   `json:"cabundle"`
}

func (r *Registry) deployCertificate(ctx context.Context, spec TLSCertificateSpec, fullchain, privateKey []byte, plan deploymentPlan, known npmCertificateIDs) (TLSDeployment, error) {
	deployment := TLSDeployment{}
	bundle := certificateBundle(fullchain, privateKey)
	leaf, chain, err := splitChain(fullchain)
	if err != nil {
		return deployment, err
	}
	for _, consumer := range spec.Consumers {
		var err error
		switch consumer.Kind {
		case runtime.TLSConsumerKindNPM:
			var knownID *int
			if id, ok := known.get(consumer.Name); ok {
				knownID = &id
			}
			var id int
			var identity NPMCertificateIdentity
			id, identity, err = r.npmManagedCertificate(ctx, consumer, spec.Domains, fullchain, privateKey, knownID)
			if err == nil {
				deployment.NPMCertificateID = &id
				deployment.NPMCertificateIdentity = &identity
				if deployment.NPMCertificateIDs == nil {
					deployment.NPMCertificateIDs = npmCertificateIDs{}
				}
				deployment.NPMCertificateIDs = deployment.NPMCertificateIDs.set(consumer.Name, id)
			}
		case runtime.TLSConsumerKindCaddy:
			_, err = r.commands().SSH(ctx, consumer.ConnectionRef, "deploy", bundle)
		case runtime.TLSConsumerKindCPanel:
			sites := plan[consumer.Name]
			var payload []byte
			payload, err = json.Marshal(cpanelDeploy{Sites: sites, Cert: string(leaf), Key: string(privateKey), CABundle: string(chain)})
			if err == nil {
				_, err = r.commands().SSH(ctx, consumer.ConnectionRef, "deploy", payload)
			}
			if err == nil {
				if deployment.CPanelSites == nil {
					deployment.CPanelSites = &cpanelSites{sites: map[string][]string{}}
				}
				if _, seen := deployment.CPanelSites.sites[consumer.Name]; !seen {
					deployment.CPanelSites.names = append(deployment.CPanelSites.names, consumer.Name)
				}
				deployment.CPanelSites.sites[consumer.Name] = sites
			}
		}
		if err != nil {
			return deployment, fmt.Errorf("deploy to %s (%s): %w", consumer.Name, consumer.Kind, err)
		}
	}
	return deployment, nil
}

// deployTransaction installs a certificate, proves every consumer serves it, and
// restores the previous one if either step fails.
func (r *Registry) deployTransaction(ctx context.Context, spec TLSCertificateSpec, fullchain, privateKey, previousFullchain, previousKey []byte, plan deploymentPlan, artifactSource, reason, message string, known npmCertificateIDs) (Result, error) {
	expected, err := validateCertificate(fullchain, privateKey, spec.Domains)
	if err != nil {
		return Result{}, err
	}
	deployment, err := r.deployCertificate(ctx, spec, fullchain, privateKey, plan, known)
	var status *TLSCertificateStatus
	if err == nil {
		_, status, err = r.verifyTLSDeployment(ctx, spec, expected)
	}
	if err != nil {
		var failure *ProviderError
		if !errors.As(err, &failure) {
			return Result{}, err
		}
		if _, rollback := r.deployCertificate(ctx, spec, previousFullchain, previousKey, plan, known); rollback != nil {
			return Result{}, &ProviderError{Message: fmt.Sprintf("certificate deployment failed (%v); rollback also failed (%v)", err, rollback)}
		}
		return Result{}, &ProviderError{Message: "certificate deployment failed, rollback succeeded", Err: err, Status: failure.Status}
	}
	matchEvidence(status, expected)
	status.TLSDeployment = deployment
	status.ArtifactSource = artifactSource
	status.RenewedFingerprint = expected
	return Result{Changed: true, Status: status, Conditions: []Condition{condition(runtime.ConditionReady, reason, "Every site is serving this certificate.")}, Message: message}, nil
}

func rollbackSource(spec TLSCertificateSpec) (TLSConsumer, bool) {
	for _, consumer := range spec.Consumers {
		if consumer.Kind == runtime.TLSConsumerKindCaddy {
			return consumer, true
		}
	}
	return TLSConsumer{}, false
}

func (r *Registry) caddySnapshot(ctx context.Context, caddy TLSConsumer) ([]byte, []byte, error) {
	payload, err := r.commands().SSH(ctx, caddy.ConnectionRef, "snapshot", nil)
	if err != nil {
		return nil, nil, err
	}
	return readBundle(payload)
}

func (r *Registry) applyTLSReconcile(ctx context.Context, spec TLSCertificateSpec, known npmCertificateIDs) (Result, error) {
	fullchain, privateKey, err := r.lineage(spec)
	if err != nil {
		return Result{}, err
	}
	expected, err := validateCertificate(fullchain, privateKey, spec.Domains)
	if err != nil {
		return Result{}, err
	}
	_, observed, err := r.reconcileTLS(ctx, spec)
	if err != nil {
		return Result{}, err
	}
	if consumersServe(spec, observed, expected) {
		matchEvidence(observed, expected)
		observed.ArtifactSource = "existing_lineage"
		return Result{Changed: false, Status: observed, Conditions: []Condition{condition(runtime.ConditionReady, "Verified", "Every site is serving this certificate.")}, Message: "Every site already serves this certificate."}, nil
	}
	caddy, ok := rollbackSource(spec)
	if !ok {
		return Result{}, &ProviderError{Message: "certificate reconciliation requires a rollback source"}
	}
	plan, err := r.planDeployment(ctx, spec)
	if err != nil {
		return Result{}, err
	}
	previousFullchain, previousKey, err := r.caddySnapshot(ctx, caddy)
	if err != nil {
		return Result{}, err
	}
	return r.deployTransaction(ctx, spec, fullchain, privateKey, previousFullchain, previousKey, plan, "existing_lineage", "Reconciled", "Installed again everywhere and checked. No new certificate was needed.", known)
}

func (r *Registry) renewTLS(ctx context.Context, spec TLSCertificateSpec, known npmCertificateIDs) (Result, error) {
	caddy, ok := rollbackSource(spec)
	if !ok {
		return Result{}, &ProviderError{Message: "certificate renewal requires a rollback source"}
	}
	// Before the CA is asked for anything: a target that cannot be satisfied costs nothing.
	plan, err := r.planDeployment(ctx, spec)
	if err != nil {
		return Result{}, err
	}
	previousFullchain, previousKey, err := r.caddySnapshot(ctx, caddy)
	if err != nil {
		return Result{}, err
	}
	previousFingerprint, err := validateCertificate(previousFullchain, previousKey, spec.Domains)
	if err != nil {
		return Result{}, err
	}
	fullchain, privateKey, resumed, err := r.resumableLineage(spec, previousFingerprint)
	if err != nil {
		return Result{}, err
	}
	source := "existing_lineage"
	if !resumed {
		if fullchain, privateKey, err = r.issueCertificate(ctx, spec); err != nil {
			return Result{}, err
		}
		source = "new_issuance"
	}
	return r.deployTransaction(ctx, spec, fullchain, privateKey, previousFullchain, previousKey, plan, source, "Renewed", "Certificate renewed, deployed, and verified.", known)
}

// publishTLSFacts records the reading wherever the certificate says to. It can
// only add to the report: a failure here is noted, never raised, and nothing is
// written on a dry run.
func (r *Registry) publishTLSFacts(ctx context.Context, spec TLSCertificateSpec, result Result, apply bool) Result {
	status, ok := result.Status.(*TLSCertificateStatus)
	if !apply || len(spec.PublishTo) == 0 || !ok {
		return result
	}
	desired := certificateFacts(spec, status)
	published := []PublishedFact{}
	for _, publication := range spec.PublishTo {
		if desired == nil {
			published = append(published, PublishedFact{Target: publication.Name, Written: false, Detail: "HQ has no single fingerprint for this certificate yet."})
			continue
		}
		fact, err := r.publishFacts(ctx, publication, desired, r.lineageMaterial(spec))
		if err != nil {
			fact = PublishedFact{Target: publication.Name, Written: false, Detail: err.Error()}
		}
		published = append(published, fact)
	}
	status.PublishedFacts = published
	unfiled := []string{}
	for _, item := range published {
		if !item.Written {
			unfiled = append(unfiled, item.Target)
		}
	}
	if len(unfiled) > 0 {
		result.Message += " Facts were not recorded on: " + strings.Join(unfiled, ", ") + "."
	}
	return result
}

func (r *Registry) tlsReconcile(ctx context.Context, spec TLSCertificateSpec, observed TLSCertificateObserved, apply bool) (Result, error) {
	known := npmCertificateIDsOf(spec, observed)
	var (
		result Result
		err    error
	)
	if apply {
		result, err = r.applyTLSReconcile(ctx, spec, known)
	} else {
		result, _, err = r.reconcileTLS(ctx, spec)
	}
	if err != nil {
		return Result{}, err
	}
	return withNPMCertificateIDs(r.publishTLSFacts(ctx, spec, result, apply), known), nil
}

func (r *Registry) tlsRenew(ctx context.Context, spec TLSCertificateSpec, observed TLSCertificateObserved, apply bool) (Result, error) {
	if !apply {
		return Result{Changed: true, Status: struct{}{}, Message: "Certificate would be issued, deployed, verified, and rolled back on failure."}, nil
	}
	known := npmCertificateIDsOf(spec, observed)
	result, err := r.renewTLS(ctx, spec, known)
	if err != nil {
		return Result{}, err
	}
	return withNPMCertificateIDs(r.publishTLSFacts(ctx, spec, result, true), known), nil
}

// uploadedReconcile installs a certificate HQ was given rather than one it issued.
func (r *Registry) uploadedReconcile(ctx context.Context, spec TLSCertificateSpec, observed TLSCertificateObserved, apply bool) (Result, error) {
	material := runtime.Material{}
	if spec.Material != nil {
		material = *spec.Material
	}
	if material.Fullchain == "" || material.PrivateKey == "" {
		return Result{}, &ProviderError{Message: "HQ did not supply the stored certificate; upload it again"}
	}
	domains := append([]string{}, material.Domains...)
	if !apply {
		return Result{Changed: true, Status: UploadedCertificateStatus{CertificateName: spec.CertificateName, Domains: domains}, Conditions: []Condition{condition(runtime.ConditionReady, "Planned", "Would install the certificate.")}, Message: "Would install the stored certificate."}, nil
	}
	target := TLSCertificateSpec{CertificateName: spec.CertificateName, Domains: domains, Consumers: spec.Consumers}
	plan, err := r.planDeployment(ctx, target)
	if err != nil {
		return Result{}, err
	}
	deployment, err := r.deployCertificate(ctx, target, []byte(material.Fullchain), []byte(material.PrivateKey), plan, npmCertificateIDsOf(spec, observed))
	if err != nil {
		return Result{}, err
	}
	return Result{Changed: true, Status: UploadedCertificateStatus{CertificateName: spec.CertificateName, Domains: domains, TLSDeployment: deployment}, Conditions: []Condition{condition(runtime.ConditionReady, "Installed", "Stored certificate installed.")}, Message: "Stored certificate installed."}, nil
}

// uploadedDelete removes an installed certificate from NPM, or refuses and says
// who has to remove it elsewhere: a forced command implements deploy and nothing else.
func (r *Registry) uploadedDelete(ctx context.Context, spec TLSCertificateSpec, observed TLSCertificateObserved, apply bool) (Result, error) {
	elsewhere := []string{}
	for _, consumer := range spec.Consumers {
		if consumer.Kind != runtime.TLSConsumerKindNPM {
			elsewhere = append(elsewhere, consumer.Name)
		}
	}
	slices.Sort(elsewhere)
	if len(elsewhere) > 0 {
		return Result{}, &ProviderError{Message: "HQ can only remove this from Nginx Proxy Manager; take it off " + strings.Join(elsewhere, ", ") + " by hand first, then remove those targets from this resource"}
	}
	base, headers, err := r.npmSession(ctx, "")
	if err != nil {
		return Result{}, err
	}
	certificates, err := r.npmCertificateList(ctx, base, headers)
	if err != nil {
		return Result{}, err
	}
	installed := map[int]bool{}
	for _, item := range npmCertificateIDsOf(spec, observed) {
		installed[item.ID] = true
	}
	matches := []int{}
	for _, item := range certificates {
		if installed[item.ID] {
			matches = append(matches, item.ID)
		}
	}
	if len(matches) == 0 {
		wanted := map[string]bool{}
		for _, consumer := range spec.Consumers {
			wanted[npmCertificateName(consumer)] = true
		}
		named := []string{}
		for _, item := range certificates {
			if wanted[item.NiceName] {
				named = append(named, item.NiceName)
			}
		}
		slices.Sort(named)
		if len(named) > 0 {
			return Result{}, &ProviderError{Message: "NPM holds " + strings.Join(named, ", ") + ", but HQ has no record of installing it, so it was not removed; remove it in NPM if it is HQ's, then remove this again"}
		}
		return Result{Changed: false, Status: CertificateRemoval{Removed: true}, Conditions: []Condition{condition(runtime.ConditionReady, "Absent", "No such certificate in NPM.")}, Message: "Certificate was already absent from NPM."}, nil
	}
	hosts, err := r.npmProxyHostList(ctx, base, headers)
	if err != nil {
		return Result{}, err
	}
	identifiers := map[int]bool{}
	for _, id := range matches {
		identifiers[id] = true
	}
	stillBound := []string{}
	for _, host := range hosts {
		if identifiers[int(host.CertificateId)] {
			stillBound = append(stillBound, host.DomainNames...)
		}
	}
	slices.Sort(stillBound)
	if len(stillBound) > 0 {
		return Result{}, &ProviderError{Message: "still serving " + strings.Join(stillBound, ", ") + "; point those at another certificate before removing this one"}
	}
	if apply {
		for _, id := range matches {
			if _, err := r.HTTP.Request(ctx, base+"/nginx/certificates/"+itoa(int64(id)), "DELETE", headers, nil); err != nil {
				return Result{}, err
			}
		}
	}
	return Result{Changed: true, Status: CertificateRemoval{Removed: true}, Conditions: []Condition{condition(runtime.ConditionReady, "Removed", "Certificate removed from NPM.")}, Message: "Certificate removed from NPM."}, nil
}
