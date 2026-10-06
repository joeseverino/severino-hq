package providers

import (
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"

	"github.com/joeseverino/severino-hq/controller/runtime"
	tsapi "tailscale.com/client/tailscale/v2"
)

// tailnetPolicyDocument is the tailnet policy file, an operator-authored JSON
// object: compared, validated and written back verbatim, so it travels as raw
// JSON. view is the typed reading of the parts HQ looks at.
type tailnetPolicyDocument json.RawMessage

// errPolicyNotObject refuses a policy that is valid JSON but not an object.
var errPolicyNotObject = &ProviderError{Message: "the policy is not a JSON object"}

// policyDocument checks raw is a JSON object and returns it as a policy.
func policyDocument(raw []byte) (tailnetPolicyDocument, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		if _, ok := errors.AsType[*json.SyntaxError](err); ok {
			return nil, &ProviderError{Message: "the policy is not readable JSON", Err: err}
		}
		return nil, errPolicyNotObject
	}
	if fields == nil {
		return nil, errPolicyNotObject
	}
	return tailnetPolicyDocument(raw), nil
}

type tailnetPolicyTest struct {
	Src    string   `json:"src"`
	Proto  string   `json:"proto"`
	Accept []string `json:"accept"`
	Deny   []string `json:"deny"`
}

type tailnetPolicyView struct {
	Tests     []tailnetPolicyTest  `json:"tests"`
	Groups    map[string][]string  `json:"groups"`
	TagOwners map[string][]string  `json:"tagOwners"`
	Hosts     map[string]string    `json:"hosts"`
	Grants    []tailnetPolicyGrant `json:"grants"`
	SSH       []tailnetPolicySSH   `json:"ssh"`
	NodeAttrs []struct {
		App struct {
			Connectors []struct {
				Name       string   `json:"name"`
				Connectors []string `json:"connectors"`
				Domains    []string `json:"domains"`
			} `json:"tailscale.com/app-connectors"`
		} `json:"app"`
	} `json:"nodeAttrs"`
}

type tailnetPolicyGrant struct {
	Src []string `json:"src"`
	Dst []string `json:"dst"`
	IP  []string `json:"ip"`
}

type tailnetPolicySSH struct {
	Action string   `json:"action"`
	Src    []string `json:"src"`
	Dst    []string `json:"dst"`
	Users  []string `json:"users"`
}

// view reads the parts HQ looks at; a part of the wrong shape is an error.
func (d tailnetPolicyDocument) view() (tailnetPolicyView, error) {
	var view tailnetPolicyView
	if err := json.Unmarshal(d, &view); err != nil {
		return view, &ProviderError{Message: "the policy does not have the shape HQ reads", Err: err}
	}
	return view, nil
}

// tests is the document's tests verbatim, or [] when it has none.
func (d tailnetPolicyDocument) tests() json.RawMessage {
	var document struct {
		Tests json.RawMessage `json:"tests"`
	}
	if json.Unmarshal(d, &document) != nil || !hasTests(document.Tests) {
		return json.RawMessage("[]")
	}
	return document.Tests
}

// tested is whether the document carries at least one test.
func (d tailnetPolicyDocument) tested() bool { return hasTests(d.tests()) }

func hasTests(raw json.RawMessage) bool {
	var tests []json.RawMessage
	return json.Unmarshal(raw, &tests) == nil && len(tests) > 0
}

// pretty is the document as HQ stores it: keys sorted, two-space indent.
func (d tailnetPolicyDocument) pretty() string {
	var value any
	decoder := json.NewDecoder(bytes.NewReader(d))
	decoder.UseNumber()
	if decoder.Decode(&value) != nil {
		return string(d)
	}
	var out bytes.Buffer
	encoder := json.NewEncoder(&out)
	encoder.SetEscapeHTML(false)
	encoder.SetIndent("", "  ")
	if encoder.Encode(value) != nil {
		return string(d)
	}
	return strings.TrimSuffix(out.String(), "\n")
}

// samePolicy is whether two documents say the same thing, whatever their
// formatting or key order.
func samePolicy(a, b tailnetPolicyDocument) bool {
	var left, right any
	if json.Unmarshal(a, &left) != nil || json.Unmarshal(b, &right) != nil {
		return false
	}
	return reflect.DeepEqual(left, right)
}

func (c tailnetClient) tailnetPolicy(ctx context.Context) (tailnetPolicyDocument, error) {
	document, _, err := c.tailnetPolicyVersion(ctx)
	return document, err
}

// tailnetPolicyVersion is the policy file and its ETag, from one read.
func (c tailnetClient) tailnetPolicyVersion(ctx context.Context) (tailnetPolicyDocument, string, error) {
	raw, etag, err := c.callHeader(ctx, tailnet("acl"), map[string]string{"Accept": "application/json"}, "etag")
	if code := httpStatus(err); code != 0 {
		return nil, "", tailnetRefused("the policy read", "policy_file:read", code)
	}
	if err != nil {
		return nil, etag, fmt.Errorf("read the tailnet policy: %w", err)
	}
	document, err := policyDocument(raw)
	return document, etag, err
}

type testKey struct{ src, proto string }

type testEntry struct {
	accept map[string]bool
	deny   map[string]bool
}

func policyTests(policy tailnetPolicyView) map[testKey]*testEntry {
	found := map[testKey]*testEntry{}
	for _, test := range policy.Tests {
		key := testKey{src: test.Src, proto: test.Proto}
		entry := found[key]
		if entry == nil {
			entry = &testEntry{accept: map[string]bool{}, deny: map[string]bool{}}
			found[key] = entry
		}
		for _, dst := range test.Accept {
			entry.accept[dst] = true
		}
		for _, dst := range test.Deny {
			entry.deny[dst] = true
		}
	}
	return found
}

func sortedTestKeys(tests map[testKey]*testEntry) []testKey {
	keys := slices.AppendSeq([]testKey{}, maps.Keys(tests))
	slices.SortFunc(keys, func(a, b testKey) int {
		return cmp.Or(cmp.Compare(a.src, b.src), cmp.Compare(a.proto, b.proto))
	})
	return keys
}

func refuseWeakerTests(live, document tailnetPolicyView) error {
	wanted := policyTests(document)
	held := policyTests(live)
	if len(wanted) == 0 {
		return &ProviderError{Message: "the declared policy carries no tests, so tailscale's check would pass it whatever it grants; not applied"}
	}
	for _, pair := range sortedTestKeys(held) {
		if _, ok := wanted[pair]; !ok {
			overProto := ""
			if pair.proto != "" {
				overProto = " over " + pair.proto
			}
			return &ProviderError{Message: fmt.Sprintf("the declared policy drops the tests for %q%s, which removes the check they made; not applied", pair.src, overProto)}
		}
	}
	for _, pair := range sortedTestKeys(held) {
		dropped := []string{}
		for dst := range held[pair].deny {
			if !wanted[pair].deny[dst] {
				dropped = append(dropped, dst)
			}
		}
		if len(dropped) > 0 {
			slices.Sort(dropped)
			return &ProviderError{Message: fmt.Sprintf("the declared policy no longer tests that %q is denied %q; a live deny is kept, so not applied", pair.src, dropped[0])}
		}
	}
	return nil
}

// policyPassesItsTests asks Tailscale to run the document's tests. An empty
// answer passes; anything else is Tailscale's account of what failed.
func (c tailnetClient) policyPassesItsTests(ctx context.Context, document tailnetPolicyDocument) error {
	raw, err := c.call(ctx, "POST", tailnet("acl/validate"), jsonBody, json.RawMessage(document))
	if err != nil {
		return fmt.Errorf("check the policy: %w", err)
	}
	if len(raw) == 0 {
		return nil
	}
	var verdict tsapi.APIError
	if err := json.Unmarshal(raw, &verdict); err != nil {
		return &ProviderError{Message: "tailscale's policy check is unreadable", Err: err}
	}
	if verdict.Message == "" && len(verdict.Data) == 0 {
		return nil
	}
	said := []string{}
	if verdict.Message != "" {
		said = append(said, verdict.Message)
	}
	for _, data := range verdict.Data {
		said = append(said, data.User+": "+strings.Join(data.Errors, ", "))
	}
	return &ProviderError{Message: "the declared policy does not pass its own tests; not applied: " + runtime.Clip(strings.Join(said, "; "), runtime.VerdictLimit)}
}

// writeTailnetPolicy writes the policy, conditional on the version the checks
// ran against: etag comes from that same read, so a change made in between
// refuses the write (412). Without one nothing is written.
func (c tailnetClient) writeTailnetPolicy(ctx context.Context, document tailnetPolicyDocument, etag string) error {
	if etag == "" {
		return errNoPolicyVersion
	}
	headers := map[string]string{"Content-Type": "application/json", "If-Match": etag}
	_, err := c.call(ctx, "POST", tailnet("acl"), headers, json.RawMessage(document))
	switch code := httpStatus(err); {
	case code == 412:
		return errPolicyChanged
	case code != 0:
		return &ProviderError{Message: fmt.Sprintf("tailscale refused the policy (%d)", code), Failure: runtime.StatusFailure(code), HTTPStatus: code}
	case err != nil:
		return fmt.Errorf("write the tailnet policy: %w", err)
	}
	return nil
}

var (
	// errNoPolicyVersion refuses a write when the read gave no ETag to hold it to.
	errNoPolicyVersion = &ProviderError{Message: "tailscale did not say which version of the policy it holds, so the policy was not written"}
	// errPolicyChanged is a write refused because the policy changed since the checked read.
	errPolicyChanged = &ProviderError{Message: "the policy changed elsewhere since HQ read it; not applied, read it again and make the change on top"}
)

func currentPolicy(document tailnetPolicyDocument) Result {
	status := TailnetPolicyStatus{Applied: true, Document: document.pretty()}
	if !document.tested() {
		return Result{
			Status: status,
			Conditions: []Condition{{
				Type:    runtime.ConditionReady,
				Status:  false,
				Reason:  "Untested",
				Message: "The policy is as HQ set it and has no tests, so nothing checks what it allows.",
			}},
			Message: "Tailnet policy is current and untested.",
		}
	}
	return Result{Status: status, Conditions: []Condition{condition(runtime.ConditionReady, "Reconciled", "The policy is as HQ set it.")}, Message: "Tailnet policy is current."}
}

func (r *Registry) tailnetPolicyReconcile(ctx context.Context, spec TailnetPolicySpec, _ struct{}, apply bool) (Result, error) {
	wanted := strings.TrimSpace(spec.Document)
	if wanted == "" {
		return Result{Status: struct{}{}, Message: "HQ holds no policy, so there is nothing to apply."}, nil
	}
	document, err := policyDocument([]byte(wanted))
	if err != nil {
		return Result{}, fmt.Errorf("declared policy: %w", err)
	}
	declared, err := document.view()
	if err != nil {
		return Result{}, fmt.Errorf("declared policy: %w", err)
	}
	client, err := r.tailnetClient(ctx, spec.ConnectionRef)
	if err != nil {
		return Result{}, err
	}
	live, etag, err := client.tailnetPolicyVersion(ctx)
	if err != nil {
		return Result{}, err
	}
	if samePolicy(live, document) {
		return currentPolicy(document), nil
	}
	held, err := live.view()
	if err != nil {
		return Result{}, fmt.Errorf("live policy: %w", err)
	}
	if err := refuseWeakerTests(held, declared); err != nil {
		return Result{}, err
	}
	if err := client.policyPassesItsTests(ctx, document); err != nil {
		return Result{}, err
	}
	if !apply {
		return Result{Changed: true, Status: struct{}{}, Message: "The policy passes its own tests and would be applied."}, nil
	}
	if err := client.writeTailnetPolicy(ctx, document, etag); err != nil {
		return Result{}, err
	}
	applied, err := client.tailnetPolicy(ctx)
	if err != nil {
		return Result{}, err
	}
	return Result{
		Changed:    true,
		Status:     TailnetPolicyStatus{Applied: true, Document: applied.pretty()},
		Conditions: []Condition{condition(runtime.ConditionReady, "Reconciled", "The policy is as HQ set it.")},
		Message:    "Tailnet policy applied after its own tests passed.",
	}, nil
}

// tailnetLockStatus is `tailscale lock status --json`.
type tailnetLockStatus struct {
	Enabled       bool              `json:"Enabled"`
	NodeKeySigned bool              `json:"NodeKeySigned"`
	TrustedKeys   []json.RawMessage `json:"TrustedKeys"`
	FilteredPeers []struct {
		Name     string `json:"Name"`
		StableID string `json:"StableID"`
	} `json:"FilteredPeers"`
}

type TailnetLock struct {
	Enabled       bool     `json:"enabled"`
	NodeKeySigned bool     `json:"node_key_signed"`
	TrustedKeys   int      `json:"trusted_keys"`
	LockedOut     []string `json:"locked_out"`
}

// tailnetLockRef is the lock reading, or {} when this controller has none.
type tailnetLockRef struct{ *TailnetLock }

func (l tailnetLockRef) MarshalJSON() ([]byte, error) {
	if l.TailnetLock == nil {
		return []byte("{}"), nil
	}
	return json.Marshal(l.TailnetLock)
}

func (r *Registry) tailnetLock() tailnetLockRef {
	lockFile := r.Env["SEVERINO_TAILNET_LOCK"]
	if lockFile == "" {
		return tailnetLockRef{}
	}
	data, err := os.ReadFile(filepath.Clean(lockFile))
	if err != nil {
		return tailnetLockRef{}
	}
	var status tailnetLockStatus
	if err := json.Unmarshal(data, &status); err != nil {
		return tailnetLockRef{}
	}
	lockedOut := []string{}
	for _, peer := range status.FilteredPeers {
		name := peer.Name
		if name == "" {
			name = peer.StableID
		}
		lockedOut = append(lockedOut, name)
	}
	slices.Sort(lockedOut)
	return tailnetLockRef{&TailnetLock{Enabled: status.Enabled, NodeKeySigned: status.NodeKeySigned, TrustedKeys: len(status.TrustedKeys), LockedOut: lockedOut}}
}

type TailnetGroup struct {
	Name    string   `json:"name"`
	Members []string `json:"members"`
}
type TailnetTag struct {
	Name   string   `json:"name"`
	Owners []string `json:"owners"`
}
type TailnetGrant struct {
	Src []string `json:"src"`
	Dst []string `json:"dst"`
	IP  []string `json:"ip"`
}
type TailnetService struct {
	Name      string   `json:"name"`
	Addresses []string `json:"addresses"`
	Comment   string   `json:"comment"`
	Ports     []string `json:"ports"`
}
type TailnetSSHRule struct {
	Action string   `json:"action"`
	Src    []string `json:"src"`
	Dst    []string `json:"dst"`
	Users  []string `json:"users"`
}
type TailnetAppConnector struct {
	Name       string   `json:"name"`
	Connectors []string `json:"connectors"`
	Domains    []string `json:"domains"`
}

// TailscalePolicyRecord is the policy as a sweep reports it. Settings and DNS
// carry Tailscale's answers verbatim; a refused one is {}.
type TailscalePolicyRecord struct {
	Record        string                `json:"record"`
	Document      string                `json:"document"`
	Hosts         map[string]string     `json:"hosts"`
	Settings      json.RawMessage       `json:"settings"`
	DNS           json.RawMessage       `json:"dns"`
	Groups        []TailnetGroup        `json:"groups"`
	Tags          []TailnetTag          `json:"tags"`
	Grants        []TailnetGrant        `json:"grants"`
	Tests         json.RawMessage       `json:"tests"`
	Lock          tailnetLockRef        `json:"lock"`
	Services      []TailnetService      `json:"services"`
	AppConnectors []TailnetAppConnector `json:"app_connectors"`
	SSHRules      []TailnetSSHRule      `json:"ssh_rules"`
}

type tailnetServices struct {
	VIPServices []struct {
		Name    string   `json:"name"`
		Addrs   []string `json:"addrs"`
		Comment string   `json:"comment"`
		Ports   []string `json:"ports"`
	} `json:"vipServices"`
}

// tailnetPart is one tailnet read for a declared part of the policy record:
// an object, kept verbatim.
func (c tailnetClient) tailnetPart(ctx context.Context, path string) (map[string]json.RawMessage, error) {
	raw, err := c.call(ctx, "GET", tailnet(path), nil, nil)
	if code := httpStatus(err); code != 0 {
		return nil, &ProviderError{Message: fmt.Sprintf("/%s answered %d", path, code), Failure: runtime.StatusFailure(code), HTTPStatus: code}
	}
	if err != nil {
		return nil, fmt.Errorf("read /%s: %w", path, err)
	}
	return decodeTailnet[map[string]json.RawMessage](raw, "/"+path)
}

func appConnectors(policy tailnetPolicyView) []TailnetAppConnector {
	found := []TailnetAppConnector{}
	for _, attr := range policy.NodeAttrs {
		for _, declared := range attr.App.Connectors {
			found = append(found, TailnetAppConnector{Name: declared.Name, Connectors: sortedCopy(declared.Connectors), Domains: sortedCopy(declared.Domains)})
		}
	}
	return found
}

func sortedKeys[V any](m map[string]V) []string {
	keys := slices.AppendSeq([]string{}, maps.Keys(m))
	slices.Sort(keys)
	return keys
}

func (r *Registry) tailnetPolicyInventory(ctx context.Context) ([]any, error) {
	client, err := r.tailnetClient(ctx, "")
	if err != nil {
		return nil, err
	}
	document, err := client.tailnetPolicy(ctx)
	if err != nil {
		return nil, err
	}
	policy, err := document.view()
	if err != nil {
		return nil, err
	}

	// Each part keeps what was read before a refusal stopped it.
	read := map[runtime.ReadingPartName]map[string]json.RawMessage{}
	for _, part := range []struct {
		name  runtime.ReadingPartName
		paths []string
	}{
		{runtime.PartSettings, []string{"settings"}},
		{runtime.PartDNS, []string{"dns/preferences", "dns/nameservers", "dns/searchpaths"}},
		{runtime.PartServices, []string{"services"}},
	} {
		merged := map[string]json.RawMessage{}
		for _, path := range part.paths {
			fields, err := client.tailnetPart(ctx, path)
			if err != nil {
				refuse(ctx, part.name, "", "", err)
				break
			}
			maps.Copy(merged, fields)
		}
		read[part.name] = merged
	}
	settings, _ := json.Marshal(read["settings"])
	dnsPart, _ := json.Marshal(read["dns"])
	var services tailnetServices
	if raw, ok := read["services"]["vipServices"]; ok {
		if err := json.Unmarshal(raw, &services.VIPServices); err != nil {
			refuse(ctx, runtime.PartServices, "", "", &ProviderError{Message: "the services answer is unreadable", Err: err})
		}
	}

	hosts := map[string]string{}
	maps.Copy(hosts, policy.Hosts)
	groups := []TailnetGroup{}
	for _, name := range sortedKeys(policy.Groups) {
		groups = append(groups, TailnetGroup{Name: name, Members: sortedCopy(policy.Groups[name])})
	}
	tags := []TailnetTag{}
	for _, name := range sortedKeys(policy.TagOwners) {
		tags = append(tags, TailnetTag{Name: name, Owners: sortedCopy(policy.TagOwners[name])})
	}
	grants := []TailnetGrant{}
	for _, grant := range policy.Grants {
		grants = append(grants, TailnetGrant{Src: sortedCopy(grant.Src), Dst: sortedCopy(grant.Dst), IP: sortedCopy(grant.IP)})
	}
	serviceRecords := []TailnetService{}
	for _, service := range services.VIPServices {
		serviceRecords = append(serviceRecords, TailnetService{Name: service.Name, Addresses: sortedCopy(service.Addrs), Comment: service.Comment, Ports: sortedCopy(service.Ports)})
	}
	sshRules := []TailnetSSHRule{}
	for _, rule := range policy.SSH {
		sshRules = append(sshRules, TailnetSSHRule{Action: rule.Action, Src: sortedCopy(rule.Src), Dst: sortedCopy(rule.Dst), Users: sortedCopy(rule.Users)})
	}

	return []any{TailscalePolicyRecord{
		Record:        "policy",
		Document:      document.pretty(),
		Hosts:         hosts,
		Settings:      settings,
		DNS:           dnsPart,
		Groups:        groups,
		Tags:          tags,
		Grants:        grants,
		Tests:         document.tests(),
		Lock:          r.tailnetLock(),
		Services:      serviceRecords,
		AppConnectors: appConnectors(policy),
		SSHRules:      sshRules,
	}}, nil
}

type tailnetPreview struct {
	Matches []struct {
		Users      []string `json:"users"`
		Ports      []string `json:"ports"`
		LineNumber *int     `json:"lineNumber"`
	} `json:"matches"`
}

// reachByDevice asks Tailscale who may reach each device's IPv4 address on
// the ports worth asking about under the live policy.
func (r *Registry) reachByDevice(ctx context.Context, devices []TailscaleDeviceRecord) map[string][]TailnetReach {
	client, err := r.tailnetClient(ctx, "")
	if err != nil {
		return map[string][]TailnetReach{}
	}
	document, err := client.tailnetPolicy(ctx)
	if err != nil {
		return map[string][]TailnetReach{}
	}
	view, err := document.view()
	if err != nil {
		return map[string][]TailnetReach{}
	}
	members := view.Groups
	flatten := func(names []string) []string {
		set := map[string]bool{}
		for _, name := range names {
			if group, ok := members[name]; ok {
				for _, member := range group {
					set[member] = true
				}
			} else {
				set[name] = true
			}
		}
		return sortedKeys(set)
	}

	asking := r.portsWorthAsking(ctx)
	found := map[string][]TailnetReach{}
	for _, device := range devices {
		address := ""
		for _, candidate := range device.Addresses {
			if !strings.Contains(candidate, ":") {
				address = candidate
				break
			}
		}
		if address == "" {
			continue
		}
		for _, port := range asking {
			target := fmt.Sprintf("%s:%d", address, port)
			raw, err := client.call(ctx, "POST", tailnet("acl/preview?type=ipport&previewFor="+url.QueryEscape(target)), jsonBody, json.RawMessage(document))
			if err != nil {
				continue
			}
			preview, err := decodeTailnet[tailnetPreview](raw, "the policy preview")
			if err != nil {
				continue
			}
			who := map[string]bool{}
			rules := []TailnetReachRule{}
			for _, match := range preview.Matches {
				for _, user := range match.Users {
					who[user] = true
				}
				rules = append(rules, TailnetReachRule{Who: sortedCopy(match.Users), To: sortedCopy(match.Ports), Line: match.LineNumber})
			}
			if len(who) > 0 {
				found[device.Name] = append(found[device.Name], TailnetReach{Port: port, Who: flatten(sortedKeys(who)), Rules: rules})
			}
		}
	}
	return found
}

// tailnetBasePorts are asked about on every device: the well-known SSH, DNS,
// HTTP and TLS ports. The rest are derived from what this estate declares.
var tailnetBasePorts = []int{standardSSHPort, dnsPort, httpPort, tlsPort}

// portsWorthAsking is the base ports, the port each SSH connection declares,
// and the ports this estate's containers publish. Without Portainer, or with
// it not answering, the rest still applies.
func (r *Registry) portsWorthAsking(ctx context.Context) []int {
	seen := map[int]bool{}
	for _, port := range tailnetBasePorts {
		seen[port] = true
	}
	for _, ref := range r.Env.SSHRefs() {
		if target, err := r.Env.SSH(ref); err == nil {
			seen[target.Port] = true
		}
	}
	if r.PublishedContainers != nil {
		if containers, err := r.PublishedContainers(ctx); err == nil {
			for _, container := range containers {
				for _, port := range container.Ports {
					if port > 0 && port < 65536 {
						seen[port] = true
					}
				}
			}
		}
	}
	ports := make([]int, 0, len(seen))
	for port := range seen {
		ports = append(ports, port)
	}
	slices.Sort(ports)
	return ports
}
