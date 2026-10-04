package providers

import (
	"context"
	"errors"
	"sort"
	"strings"
)

func (r *Registry) admitTLS() {
	r.action(certificateKind, "reconcile", r.tlsReconcile)
	r.action(certificateKind, "renew", r.tlsRenew)
	r.action(uploadedCertificateKind, "reconcile", r.uploadedReconcile)
	r.action(uploadedCertificateKind, "delete", r.uploadedDelete)
	r.probe("onepassword", r.probeOnePassword)
}

// NeedsMaterial says whether an action on kind needs HQ's stored certificate material.
func (r *Registry) NeedsMaterial(kind string) bool {
	return kind == uploadedCertificateKind
}

func errorText(err error) string {
	var provider *ProviderError
	if errors.As(err, &provider) {
		return provider.Message
	}
	return err.Error()
}

// cpanelSites is the cPanel sites a consumer installs on, decided before anything is
// issued: the sites serving its install names, else every site serving a checked name.
func (r *Registry) cpanelSitesFor(ctx context.Context, consumer TLSConsumer) ([]string, error) {
	raw, err := r.commands().SSH(ctx, consumer.ConnectionRef, "sites", nil)
	if err != nil {
		return nil, err
	}
	if len(raw) == 0 {
		raw = []byte("{}")
	}
	answer, err := parsePy(raw)
	if err != nil {
		return nil, &ProviderError{Message: consumer.Name + " returned a site list HQ could not read."}
	}
	var sites *pyValue
	if answer.object {
		if at := indexOf(answer.keys, "sites"); at >= 0 {
			sites = &answer.values[at]
		}
	}
	if sites == nil || !sites.object || len(sites.keys) == 0 {
		return nil, &ProviderError{Message: consumer.Name + " reported no sites."}
	}
	namesOf := map[string][]string{}
	siteOf := map[string]string{}
	for i, site := range sites.keys {
		names := []string{site}
		for _, name := range sites.values[i].array {
			if name.text != nil {
				names = append(names, *name.text)
			}
		}
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
	sort.Strings(notHosted)
	if len(notHosted) > 0 {
		return nil, &ProviderError{Message: consumer.Name + " does not serve " + strings.Join(notHosted, ", ") + ". Remove the name from the target, or add it to the hosting account."}
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
		return nil, &ProviderError{Message: consumer.Name + " would be checked at " + strings.Join(unserved, ", ") + " but installs only on " + strings.Join(chosen, ", ") + ". Add those names to the target's install list, or leave the list empty to install on every site that serves a checked name."}
	}
	if len(chosen) == 0 {
		return nil, &ProviderError{Message: consumer.Name + " has no site to install on."}
	}
	return chosen, nil
}

// deploymentPlan is everything a deploy needs from the consumers, asked before a
// certificate is requested or any consumer is touched.
type deploymentPlan map[string][]string

func (r *Registry) planDeployment(ctx context.Context, spec TLSCertificateSpec) (deploymentPlan, error) {
	plan := deploymentPlan{}
	for _, consumer := range spec.Consumers {
		if consumer.Kind == "cpanel" {
			sites, err := r.cpanelSitesFor(ctx, consumer)
			if err != nil {
				return nil, err
			}
			plan[consumer.Name] = sites
		}
	}
	return plan, nil
}

// cpanelPayload is the deploy command's JSON, compact and ASCII-escaped as json.dumps writes it.
func cpanelPayload(sites []string, leaf, privateKey, chain []byte) []byte {
	var out strings.Builder
	out.WriteString(`{"sites":[`)
	for i, site := range sites {
		if i > 0 {
			out.WriteByte(',')
		}
		writePyString(&out, site)
	}
	out.WriteString(`],"cert":`)
	writePyString(&out, string(leaf))
	out.WriteString(`,"key":`)
	writePyString(&out, string(privateKey))
	out.WriteString(`,"cabundle":`)
	writePyString(&out, string(chain))
	out.WriteByte('}')
	return []byte(out.String())
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
		case "npm":
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
		case "caddy":
			_, err = r.commands().SSH(ctx, consumer.ConnectionRef, "deploy", bundle)
		case "cpanel":
			sites := plan[consumer.Name]
			_, err = r.commands().SSH(ctx, consumer.ConnectionRef, "deploy", cpanelPayload(sites, leaf, privateKey, chain))
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
			if !isProviderError(err) {
				return deployment, err
			}
			return deployment, &ProviderError{Message: "TLS deployment failed for " + consumer.Name + " (" + consumer.Kind + "): " + errorText(err)}
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
			return Result{}, &ProviderError{Message: "Certificate deployment failed (" + failure.Message + "); rollback also failed (" + errorText(rollback) + ")."}
		}
		return Result{}, &ProviderError{Message: "Certificate deployment failed: " + failure.Message + " Rollback succeeded.", Status: failure.Status}
	}
	matchEvidence(status, expected)
	status.TLSDeployment = deployment
	status.ArtifactSource = artifactSource
	status.RenewedFingerprint = expected
	return Result{Changed: true, Status: status, Conditions: []Condition{condition("Ready", reason, "All TLS consumers serve the certificate.")}, Message: message}, nil
}

func rollbackSource(spec TLSCertificateSpec) (TLSConsumer, bool) {
	for _, consumer := range spec.Consumers {
		if consumer.Kind == "caddy" {
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
		return Result{Changed: false, Status: observed, Conditions: []Condition{condition("Ready", "Verified", "All TLS consumers match.")}, Message: "Certificate consumers already match the managed lineage."}, nil
	}
	caddy, ok := rollbackSource(spec)
	if !ok {
		return Result{}, &ProviderError{Message: "Certificate reconciliation requires a rollback source."}
	}
	plan, err := r.planDeployment(ctx, spec)
	if err != nil {
		return Result{}, err
	}
	previousFullchain, previousKey, err := r.caddySnapshot(ctx, caddy)
	if err != nil {
		return Result{}, err
	}
	return r.deployTransaction(ctx, spec, fullchain, privateKey, previousFullchain, previousKey, plan, "existing_lineage", "Reconciled", "Certificate redistributed and verified without issuance.", known)
}

func (r *Registry) renewTLS(ctx context.Context, spec TLSCertificateSpec, known npmCertificateIDs) (Result, error) {
	caddy, ok := rollbackSource(spec)
	if !ok {
		return Result{}, &ProviderError{Message: "Certificate renewal requires a rollback source."}
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
			fact = PublishedFact{Target: publication.Name, Written: false, Detail: errorText(err)}
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

func (r *Registry) tlsReconcile(ctx context.Context, rawSpec, rawObserved Object, apply bool) (Result, error) {
	spec, err := decodePayload[TLSCertificateSpec](rawSpec)
	if err != nil {
		return Result{}, &ProviderError{Message: "Certificate spec was invalid."}
	}
	known := npmCertificateIDsOf(spec, rawObserved)
	var result Result
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

func (r *Registry) tlsRenew(ctx context.Context, rawSpec, rawObserved Object, apply bool) (Result, error) {
	if !apply {
		return Result{Changed: true, Status: struct{}{}, Message: "Certificate would be issued, deployed, verified, and rolled back on failure."}, nil
	}
	spec, err := decodePayload[TLSCertificateSpec](rawSpec)
	if err != nil {
		return Result{}, &ProviderError{Message: "Certificate spec was invalid."}
	}
	known := npmCertificateIDsOf(spec, rawObserved)
	result, err := r.renewTLS(ctx, spec, known)
	if err != nil {
		return Result{}, err
	}
	return withNPMCertificateIDs(r.publishTLSFacts(ctx, spec, result, true), known), nil
}

// uploadedReconcile installs a certificate HQ was given rather than one it issued.
func (r *Registry) uploadedReconcile(ctx context.Context, rawSpec, rawObserved Object, apply bool) (Result, error) {
	spec, err := decodePayload[TLSCertificateSpec](rawSpec)
	if err != nil {
		return Result{}, &ProviderError{Message: "Certificate spec was invalid."}
	}
	material := UploadedMaterial{}
	if spec.Material != nil {
		material = *spec.Material
	}
	if material.Fullchain == "" || material.PrivateKey == "" {
		return Result{}, &ProviderError{Message: "HQ did not supply the stored certificate. Upload it again."}
	}
	domains := append([]string{}, material.Domains...)
	if !apply {
		return Result{Changed: true, Status: UploadedCertificateStatus{CertificateName: spec.CertificateName, Domains: domains}, Conditions: []Condition{condition("Ready", "Planned", "Would install the certificate.")}, Message: "Would install the stored certificate."}, nil
	}
	target := TLSCertificateSpec{CertificateName: spec.CertificateName, Domains: domains, Consumers: spec.Consumers}
	plan, err := r.planDeployment(ctx, target)
	if err != nil {
		return Result{}, err
	}
	deployment, err := r.deployCertificate(ctx, target, []byte(material.Fullchain), []byte(material.PrivateKey), plan, npmCertificateIDsOf(spec, rawObserved))
	if err != nil {
		return Result{}, err
	}
	return Result{Changed: true, Status: UploadedCertificateStatus{CertificateName: spec.CertificateName, Domains: domains, TLSDeployment: deployment}, Conditions: []Condition{condition("Ready", "Installed", "Stored certificate installed.")}, Message: "Stored certificate installed."}, nil
}

// uploadedDelete removes an installed certificate from NPM, or refuses and says
// who has to remove it elsewhere: a forced command implements deploy and nothing else.
func (r *Registry) uploadedDelete(ctx context.Context, rawSpec, rawObserved Object, apply bool) (Result, error) {
	spec, err := decodePayload[TLSCertificateSpec](rawSpec)
	if err != nil {
		return Result{}, &ProviderError{Message: "Certificate spec was invalid."}
	}
	elsewhere := []string{}
	for _, consumer := range spec.Consumers {
		if consumer.Kind != "npm" {
			elsewhere = append(elsewhere, consumer.Name)
		}
	}
	sort.Strings(elsewhere)
	if len(elsewhere) > 0 {
		return Result{}, &ProviderError{Message: "HQ can only remove this from Nginx Proxy Manager. Take it off " + strings.Join(elsewhere, ", ") + " by hand first, then remove those targets from this resource."}
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
	for _, item := range npmCertificateIDsOf(spec, rawObserved) {
		installed[item.ID] = true
	}
	matches := []int{}
	for _, item := range certificates {
		if id, ok := jsonInt(item.ID); ok && installed[id] {
			matches = append(matches, id)
		}
	}
	if len(matches) == 0 {
		wanted := map[string]bool{}
		for _, consumer := range spec.Consumers {
			wanted[npmCertificateName(consumer)] = true
		}
		named := []string{}
		for _, item := range certificates {
			if name, ok := item.niceName(); ok && wanted[name] {
				named = append(named, name)
			}
		}
		sort.Strings(named)
		if len(named) > 0 {
			return Result{}, &ProviderError{Message: "NPM holds " + strings.Join(named, ", ") + ", but HQ has no record of installing it, so it was not removed. Remove it in NPM if it is HQ's, then remove this again."}
		}
		return Result{Changed: false, Status: CertificateRemoval{Removed: true}, Conditions: []Condition{condition("Ready", "Absent", "No such certificate in NPM.")}, Message: "Certificate was already absent from NPM."}, nil
	}
	hosts, err := r.npmCertificateHosts(ctx, base, headers)
	if err != nil {
		return Result{}, err
	}
	identifiers := map[int]bool{}
	for _, id := range matches {
		identifiers[id] = true
	}
	stillBound := []string{}
	for _, host := range hosts {
		if id, ok := jsonInt(host.CertificateID); ok && identifiers[id] {
			stillBound = append(stillBound, host.DomainNames...)
		}
	}
	sort.Strings(stillBound)
	if len(stillBound) > 0 {
		return Result{}, &ProviderError{Message: "Still serving " + strings.Join(stillBound, ", ") + ". Point those at another certificate before removing this one."}
	}
	if apply {
		for _, id := range matches {
			if _, err := r.HTTP.Request(ctx, base+"/nginx/certificates/"+itoa(int64(id)), "DELETE", headers, nil); err != nil {
				return Result{}, err
			}
		}
	}
	return Result{Changed: true, Status: CertificateRemoval{Removed: true}, Conditions: []Condition{condition("Ready", "Removed", "Certificate removed from NPM.")}, Message: "Certificate removed from NPM."}, nil
}
