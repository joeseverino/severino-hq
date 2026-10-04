package providers

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// tailnetPolicyDocument is the tailnet policy file, an operator-authored
// document: compared, validated and written back verbatim, so it travels as
// raw JSON. view is the typed reading of the parts HQ looks at.
type tailnetPolicyDocument json.RawMessage

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

func (d tailnetPolicyDocument) view() tailnetPolicyView {
	var view tailnetPolicyView
	_ = json.Unmarshal(d, &view)
	return view
}

// field is one top-level value of the document, parsed, or nil when absent.
func (d tailnetPolicyDocument) field(name string) *pyValue {
	value, err := parsePy(d)
	if err != nil || !value.object {
		return nil
	}
	if at := indexOf(value.keys, name); at >= 0 {
		return &value.values[at]
	}
	return nil
}

// tests is the document's tests verbatim, or [] when it has none.
func (d tailnetPolicyDocument) tests() json.RawMessage {
	var document struct {
		Tests json.RawMessage `json:"tests"`
	}
	if tests := d.field("tests"); tests == nil || !tests.truthy() || json.Unmarshal(d, &document) != nil {
		return json.RawMessage("[]")
	}
	return document.Tests
}

// pretty is the document as HQ stores it: json.dumps(indent=2, sort_keys=True).
func (d tailnetPolicyDocument) pretty() string {
	out, _ := pyDumps(d, 2, true)
	return out
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
	if err != nil || len(raw) == 0 {
		return nil, etag, &ProviderError{Message: "Tailscale did not return a readable policy.", Failure: networkFailure(err)}
	}
	return tailnetPolicyDocument(raw), etag, nil
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
	keys := []testKey{}
	for key := range tests {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].src != keys[j].src {
			return keys[i].src < keys[j].src
		}
		return keys[i].proto < keys[j].proto
	})
	return keys
}

func refuseWeakerTests(live, document tailnetPolicyView) error {
	wanted := policyTests(document)
	held := policyTests(live)
	if len(wanted) == 0 {
		return &ProviderError{
			Message: "The declared policy carries no tests, so Tailscale's check would pass it whatever it grants. It was not applied.",
		}
	}
	for _, pair := range sortedTestKeys(held) {
		if _, ok := wanted[pair]; !ok {
			overProto := ""
			if pair.proto != "" {
				overProto = " over " + pair.proto
			}
			return &ProviderError{
				Message: fmt.Sprintf("The declared policy drops the tests for %s%s, which removes the check they made, so it was not applied.", pyRepr(pair.src), overProto),
			}
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
			sort.Strings(dropped)
			return &ProviderError{
				Message: fmt.Sprintf("The declared policy no longer tests that %s is denied %s. A live deny is kept, so it was not applied.", pyRepr(pair.src), pyRepr(dropped[0])),
			}
		}
	}
	return nil
}

func (c tailnetClient) policyPassesItsTests(ctx context.Context, document tailnetPolicyDocument) error {
	raw, err := c.call(ctx, "POST", tailnet("acl/validate"), map[string]string{
		"Content-Type": "application/json",
	}, json.RawMessage(document))
	if err != nil {
		return &ProviderError{Message: "Tailscale could not check the policy.", Failure: networkFailure(err)}
	}
	if len(raw) == 0 {
		return nil
	}
	// An empty answer passes; anything else is Tailscale's account of what failed.
	verdict, err := parsePy(raw)
	if err != nil {
		return &ProviderError{Message: "Tailscale could not check the policy."}
	}
	if verdict.truthy() {
		said, _ := pyDumps(raw, 0, false)
		return &ProviderError{Message: "The declared policy does not pass its own tests, so it was not applied: " + runtime.Clip(said, runtime.VerdictLimit)}
	}
	return nil
}

// writeTailnetPolicy writes the policy, conditional on the version the checks
// ran against: etag comes from that same read, so a change made in between
// refuses the write (412). Without one nothing is written.
func (c tailnetClient) writeTailnetPolicy(ctx context.Context, document tailnetPolicyDocument, etag string) error {
	if etag == "" {
		return &ProviderError{Message: "Tailscale did not say which version of the policy it holds, so the policy was not written."}
	}
	headers := map[string]string{"Content-Type": "application/json", "If-Match": etag}
	_, err := c.call(ctx, "POST", tailnet("acl"), headers, json.RawMessage(document))
	switch code := httpStatus(err); {
	case code == 412:
		return &ProviderError{Message: "The policy changed somewhere else since HQ read it, so this was not applied. Read it again and make the change on top."}
	case code != 0:
		return &ProviderError{Message: fmt.Sprintf("Tailscale refused the policy (%d).", code), Failure: runtime.StatusFailure(code)}
	case err != nil:
		return &ProviderError{Message: "Tailscale did not answer the policy write.", Failure: networkFailure(err)}
	}
	return nil
}

func currentPolicy(document tailnetPolicyDocument) Result {
	status := TailnetPolicyStatus{Applied: true, Document: document.pretty()}
	if tests := document.field("tests"); tests == nil || !tests.truthy() {
		return Result{
			Status: status,
			Conditions: []Condition{{
				Type:    "Ready",
				Status:  false,
				Reason:  "Untested",
				Message: "The policy is as declared and carries no tests, so nothing checks what it grants.",
			}},
			Message: "Tailnet policy is current and untested.",
		}
	}
	return Result{Status: status, Conditions: []Condition{condition("Ready", "Reconciled", "The policy is as declared.")}, Message: "Tailnet policy is current."}
}

func (r *Registry) tailnetPolicyReconcile(ctx context.Context, spec TailnetPolicySpec, _ struct{}, apply bool) (Result, error) {
	wanted := strings.TrimSpace(spec.Document)
	if wanted == "" {
		return Result{Status: struct{}{}, Message: "No policy is declared, so there is nothing to apply."}, nil
	}
	declared, err := parsePy([]byte(wanted))
	if err != nil {
		return Result{}, &ProviderError{Message: "The declared policy is not readable JSON."}
	}
	if !declared.object {
		return Result{}, &ProviderError{Message: "The declared policy is not a JSON object."}
	}
	document := tailnetPolicyDocument(wanted)
	client, err := r.tailnetClient(ctx, spec.ConnectionRef)
	if err != nil {
		return Result{}, err
	}
	live, etag, err := client.tailnetPolicyVersion(ctx)
	if err != nil {
		return Result{}, err
	}
	if held, err := parsePy(live); err == nil && pyEqual(held, declared) {
		return currentPolicy(document), nil
	}
	if err := refuseWeakerTests(live.view(), document.view()); err != nil {
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
		Conditions: []Condition{condition("Ready", "Reconciled", "The policy is as declared.")},
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
	sort.Strings(lockedOut)
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
	keys := []string{}
	for key := range m {
		keys = append(keys, key)
	}
	sort.Strings(keys)
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
	policy := document.view()

	// Each part is what was read before any refusal, as the Python sweep keeps it.
	read := map[string]map[string]json.RawMessage{}
	for _, part := range []struct {
		name  string
		paths []string
	}{
		{"settings", []string{"settings"}},
		{"dns", []string{"dns/preferences", "dns/nameservers", "dns/searchpaths"}},
		{"services", []string{"services"}},
	} {
		merged := map[string]json.RawMessage{}
		for _, path := range part.paths {
			fields, err := client.tailnetPart(ctx, path)
			if err != nil {
				refuse(ctx, part.name, "", "", err)
				break
			}
			for key, value := range fields {
				merged[key] = value
			}
		}
		read[part.name] = merged
	}
	settings, _ := json.Marshal(read["settings"])
	dnsPart, _ := json.Marshal(read["dns"])
	var services tailnetServices
	if raw, ok := read["services"]["vipServices"]; ok {
		_ = json.Unmarshal(raw, &services.VIPServices)
	}

	hosts := map[string]string{}
	for name, address := range policy.Hosts {
		hosts[name] = address
	}
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
	members := document.view().Groups
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
			raw, err := client.call(ctx, "POST", tailnet("acl/preview?type=ipport&previewFor="+url.QueryEscape(target)), map[string]string{
				"Content-Type": "application/json",
			}, json.RawMessage(document))
			if err != nil {
				continue
			}
			preview, _ := decodeAs[tailnetPreview](raw, "")
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

// tailnetBasePorts are where the answers start; the rest are the ports this
// estate's containers publish.
var tailnetBasePorts = []int{standardSSHPort, 53, 80, 443}

// portsWorthAsking is the ports something here listens on, plus the usual few.
// Without Portainer, or with it not answering, the base set still applies.
func (r *Registry) portsWorthAsking(ctx context.Context) []int {
	seen := map[int]bool{}
	for _, port := range tailnetBasePorts {
		seen[port] = true
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
	sort.Ints(ports)
	return ports
}
