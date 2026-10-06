package providers

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/providers/npmapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// certificateCovers is TLS wildcard matching: a wildcard covers one label.
func certificateCovers(domain string, names map[string]bool) bool {
	normalized := hostname(domain)
	if names[normalized] {
		return true
	}
	_, parent, found := strings.Cut(normalized, ".")
	return found && names["*."+parent]
}

func nameSet(names []string) map[string]bool {
	set := map[string]bool{}
	for _, name := range names {
		set[name] = true
	}
	return set
}

// daysUntil is days left to the nearest day; negative only once past.
func daysUntil(when, now time.Time) int {
	left := when.Sub(now).Seconds() / 86400
	if left < 0 {
		return int(math.Floor(left))
	}
	if left > 0 {
		return max(1, int(math.Floor(left+0.5)))
	}
	return 0
}

// npmCoveredHosts is the serving proxy hosts with a name the certificate covers.
func (r *Registry) npmCoveredHosts(ctx context.Context, certificateDomains []string) ([]npmapi.ProxyHostObject, error) {
	base, headers, err := r.npmSession(ctx, "")
	if err != nil {
		return nil, err
	}
	hosts, err := r.npmProxyHostList(ctx, base, headers)
	if err != nil {
		return nil, err
	}
	names := nameSet(certificateDomains)
	covered := []npmapi.ProxyHostObject{}
	for _, host := range hosts {
		if !host.Enabled {
			continue
		}
		for _, domain := range host.DomainNames {
			if certificateCovers(domain, names) {
				covered = append(covered, host)
				break
			}
		}
	}
	return covered, nil
}

// tlsConsumerDomains is the names to read one consumer on: declared, plus what NPM routes.
func (r *Registry) tlsConsumerDomains(ctx context.Context, consumer TLSConsumer, spec TLSCertificateSpec) ([]string, error) {
	domains := append([]string{}, consumer.VerifyDomains...)
	if consumer.Kind == runtime.TLSConsumerKindNPM && consumer.DiscoverCoveredHosts {
		covered, err := r.npmCoveredHosts(ctx, spec.Domains)
		if err != nil {
			return nil, err
		}
		set := nameSet(domains)
		for _, host := range covered {
			for _, name := range host.DomainNames {
				set[name] = true
			}
		}
		domains = sortedKeys(set)
	}
	return domains, nil
}

// consumerTLSEndpoint resolves a consumer's origin without changing the name sent as SNI.
func (r *Registry) consumerTLSEndpoint(consumer TLSConsumer) (string, error) {
	switch consumer.Kind {
	case runtime.TLSConsumerKindNPM:
		connection, err := r.Supplied.For(runtime.ConnectionProviderNPM, "")
		if err != nil {
			return "", err
		}
		login, err := runtime.Need(connection.Login)
		if err != nil {
			return "", err
		}
		parsed, err := url.Parse(login.URL)
		if err != nil || parsed.Hostname() == "" {
			return "", &ProviderError{Message: "NPM origin verification endpoint is missing"}
		}
		return strings.ToLower(parsed.Hostname()), nil
	case runtime.TLSConsumerKindCaddy, runtime.TLSConsumerKindCPanel:
		target, err := r.Supplied.SSH(consumer.ConnectionRef)
		if err != nil {
			return "", err
		}
		if target.Host == "" {
			return "", &ProviderError{Message: string(consumer.Kind) + " origin verification endpoint is missing"}
		}
		return target.Host, nil
	}
	return "", nil
}

func tlsUnreachable(consumer TLSConsumer, domain, endpoint string, err error) TLSUnreachable {
	return TLSUnreachable{Consumer: consumer.Name, Domain: domain, Endpoint: endpoint, Port: strconv.Itoa(tlsPort), Reason: err.Error()}
}

func (r *Registry) readTLSConsumer(ctx context.Context, consumer TLSConsumer, domains []string) ([]TLSObservation, []TLSUnreachable) {
	connectHost, err := r.consumerTLSEndpoint(consumer)
	if err != nil {
		return nil, []TLSUnreachable{tlsUnreachable(consumer, "", "", err)}
	}
	observations := []TLSObservation{}
	unreachable := []TLSUnreachable{}
	for _, domain := range domains {
		observed, err := r.observeTLS(ctx, domain, connectHost)
		if err != nil {
			endpoint := connectHost
			if endpoint == "" {
				endpoint = domain
			}
			unreachable = append(unreachable, tlsUnreachable(consumer, domain, endpoint, err))
			continue
		}
		observed.Consumer, observed.ConsumerKind = consumer.Name, consumer.Kind
		observations = append(observations, observed)
	}
	return observations, unreachable
}

func isProviderError(err error) bool {
	var provider *ProviderError
	return errors.As(err, &provider)
}

func tlsConditions(spec TLSCertificateSpec, observations []TLSObservation, unverified []string, unreachable []TLSUnreachable, daysRemaining int) []Condition {
	conditions := []Condition{}
	fingerprints := map[string]bool{}
	for _, item := range observations {
		fingerprints[item.FingerprintSHA256] = true
	}
	if len(fingerprints) > 1 {
		conditions = append(conditions, Condition{Type: runtime.ConditionDrifted, Status: true, Reason: "ConsumerMismatch", Message: "Sites are serving different certificates."})
	}
	if daysRemaining <= spec.RenewalWindowDays {
		conditions = append(conditions, Condition{Type: runtime.ConditionDegraded, Status: true, Reason: "ExpiringSoon", Message: fmt.Sprintf("A verified TLS consumer expires in %d days.", daysRemaining)})
	}
	if len(unverified) > 0 {
		conditions = append(conditions, Condition{Type: runtime.ConditionDegraded, Status: true, Reason: "ConsumerUnverified", Message: "No name to check is set for: " + strings.Join(unverified, ", ")})
	}
	if len(unreachable) > 0 {
		missed := make([]string, 0, len(unreachable))
		for _, item := range unreachable {
			if item.Domain != "" {
				missed = append(missed, item.Domain)
			} else {
				missed = append(missed, item.Consumer)
			}
		}
		conditions = append(conditions, Condition{Type: runtime.ConditionDegraded, Status: true, Reason: "ConsumerUnreachable", Message: "Could not be read: " + strings.Join(missed, ", ")})
	}
	if len(conditions) == 0 {
		return []Condition{condition(runtime.ConditionReady, "Verified", "Every site is serving this certificate.")}
	}
	return conditions
}

// reconcileTLS reads what every consumer serves and judges it.
func (r *Registry) reconcileTLS(ctx context.Context, spec TLSCertificateSpec) (Result, *TLSCertificateStatus, error) {
	observations := []TLSObservation{}
	unverified := []string{}
	unreachable := []TLSUnreachable{}
	for _, consumer := range spec.Consumers {
		domains, err := r.tlsConsumerDomains(ctx, consumer, spec)
		if err != nil {
			return Result{}, nil, err
		}
		if len(domains) == 0 {
			unverified = append(unverified, consumer.Name)
			continue
		}
		read, missed := r.readTLSConsumer(ctx, consumer, domains)
		observations = append(observations, read...)
		unreachable = append(unreachable, missed...)
	}
	if len(observations) == 0 {
		if len(unreachable) > 0 {
			reasons := make([]string, 0, len(unreachable))
			for _, item := range unreachable {
				reasons = append(reasons, item.Reason)
			}
			return Result{}, nil, &ProviderError{Message: "no TLS consumer could be reached: " + strings.Join(reasons, "; ")}
		}
		return Result{}, nil, &ProviderError{Message: "no TLS verification domains were declared"}
	}
	soonest, newest := observations[0], observations[0]
	for _, item := range observations[1:] {
		if item.NotAfter < soonest.NotAfter {
			soonest = item
		}
		if item.NotAfter > newest.NotAfter {
			newest = item
		}
	}
	expiry, err := time.Parse(time.RFC3339, soonest.NotAfter)
	if err != nil {
		return Result{}, nil, &ProviderError{Message: "certificate expiry unreadable", Err: err}
	}
	verified := make([]string, 0, len(observations))
	for _, item := range observations {
		verified = append(verified, item.Domain)
	}
	slices.Sort(verified)
	status := &TLSCertificateStatus{
		Issuer:               newest.Issuer,
		NotAfter:             soonest.NotAfter,
		ArtifactNotAfter:     newest.NotAfter,
		CertificatePEM:       newest.certificatePEM,
		VerifiedDomains:      verified,
		Consumers:            observations,
		UnreachableConsumers: unreachable,
	}
	return Result{
		Changed:    false,
		Status:     status,
		Conditions: tlsConditions(spec, observations, unverified, unreachable, daysUntil(expiry, r.Now())),
		Message:    "Checked what each site serves.",
	}, status, nil
}

// tlsVerificationPolicy is how long to keep checking that a renewed certificate is served.
func tlsVerificationPolicy(ctx context.Context) (time.Duration, time.Duration, error) {
	policy, ok := runtime.VerificationFrom(ctx)
	if !ok {
		return 0, 0, &ProviderError{Message: "TLS renewal declares no verification policy"}
	}
	timeout, interval := policy.TimeoutSeconds, policy.IntervalSeconds
	if timeout < verificationTimeoutMin || timeout > verificationTimeoutMax || interval < verificationIntervalMin || interval > verificationIntervalMax || interval > timeout {
		return 0, 0, &ProviderError{Message: "TLS renewal verification policy is out of bounds"}
	}
	return time.Duration(timeout) * time.Second, time.Duration(interval) * time.Second, nil
}

// consumersServe is whether every consumer was read and every reading is the
// expected certificate. One matching consumer never vouches for the rest.
func consumersServe(spec TLSCertificateSpec, status *TLSCertificateStatus, expected string) bool {
	if len(status.UnreachableConsumers) > 0 {
		return false
	}
	read := map[string]bool{}
	seen := map[string]bool{}
	for _, item := range status.Consumers {
		read[item.Consumer] = true
		seen[item.FingerprintSHA256] = true
	}
	for _, consumer := range spec.Consumers {
		if !read[consumer.Name] {
			return false
		}
	}
	return len(seen) == 1 && seen[expected]
}

// unserved is each consumer not shown serving the certificate, and what was found.
func unserved(spec TLSCertificateSpec, status *TLSCertificateStatus, expected string) map[string][]string {
	found := map[string][]string{}
	stale := map[string][]string{}
	read := map[string]bool{}
	for _, item := range status.Consumers {
		read[item.Consumer] = true
		if item.FingerprintSHA256 != expected {
			stale[item.Consumer] = append(stale[item.Consumer], item.Domain)
		}
	}
	for consumer, names := range stale {
		names = append([]string{}, names...)
		slices.Sort(names)
		found[consumer] = append(found[consumer], consumer+" still serves the previous certificate at "+strings.Join(names, ", "))
	}
	missed := map[string][]string{}
	for _, item := range status.UnreachableConsumers {
		if _, ok := missed[item.Consumer]; !ok {
			missed[item.Consumer] = []string{}
		}
		if item.Domain != "" {
			missed[item.Consumer] = append(missed[item.Consumer], item.Domain)
		}
	}
	for consumer, names := range missed {
		line := consumer + " could not be read"
		if len(names) > 0 {
			names = append([]string{}, names...)
			slices.Sort(names)
			line += " at " + strings.Join(names, ", ")
		}
		found[consumer] = append(found[consumer], line)
	}
	for _, consumer := range spec.Consumers {
		if _, ok := missed[consumer.Name]; !read[consumer.Name] && !ok {
			found[consumer.Name] = append(found[consumer.Name], consumer.Name+" has no verification domain")
		}
	}
	return found
}

// verifyTLSDeployment reads every consumer until each serves the expected certificate.
func (r *Registry) verifyTLSDeployment(ctx context.Context, spec TLSCertificateSpec, expected string) (Result, *TLSCertificateStatus, error) {
	timeout, interval, err := tlsVerificationPolicy(ctx)
	if err != nil {
		return Result{}, nil, err
	}
	deadline := r.Monotonic().Add(timeout)
	for {
		result, status, err := r.reconcileTLS(ctx, spec)
		if err != nil {
			return Result{}, nil, err
		}
		if consumersServe(spec, status, expected) {
			return result, status, nil
		}
		if !r.Monotonic().Before(deadline) {
			evidence := TLSVerificationEvidence{ExpectedFingerprint: expected, Consumers: []TLSConsumerEvidence{}}
			for _, item := range status.Consumers {
				matches := item.FingerprintSHA256 == expected
				evidence.Consumers = append(evidence.Consumers, TLSConsumerEvidence{Consumer: item.Consumer, Kind: item.ConsumerKind, Domain: item.Domain, FingerprintSHA256: item.FingerprintSHA256, MatchesExpected: matches})
			}
			failing := unserved(spec, status, expected)
			consumers := make([]string, 0, len(failing))
			for consumer := range failing {
				consumers = append(consumers, consumer)
			}
			slices.Sort(consumers)
			details := []string{}
			for _, consumer := range consumers {
				details = append(details, failing[consumer]...)
			}
			return Result{}, nil, &ProviderError{
				Message: fmt.Sprintf("%d of %d TLS consumers did not activate the certificate within %ds: %s", len(failing), len(spec.Consumers), int(timeout.Seconds()), strings.Join(details, "; ")),
				Status:  evidence,
			}
		}
		if err := r.Sleep(ctx, interval); err != nil {
			return Result{}, nil, err
		}
	}
}

// matchEvidence states the expected fingerprint beside every observation.
func matchEvidence(status *TLSCertificateStatus, expected string) {
	status.ExpectedFingerprint = expected
	for i := range status.Consumers {
		matches := status.Consumers[i].FingerprintSHA256 == expected
		status.Consumers[i].MatchesExpected = &matches
	}
}
