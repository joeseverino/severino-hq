package providers

import (
	"bytes"
	"encoding/json"
	"flag"
	"os"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// recordsGolden is every record type each reader reports, every member set,
// as the controller writes it. HQ holds every member in it to the contract
// (hq/domains/control_plane/tests/test_bridge_record_names.py): one the
// contract leaves undeclared HQ drops, and the bridge's door strikes any
// named as a secret.
const recordsGolden = "testdata/golden/records.json"

var updateGolden = flag.Bool("update", false, "rewrite "+recordsGolden)

// recordTypes is each reader's record types, one value of each.
var recordTypes = map[runtime.ResourceKind][]any{
	runtime.ResourceKindAdGuardRewrite:               {AdGuardRewriteInventoryRecord{}},
	runtime.ResourceKindAdGuardClient:                {AdGuardPersistentClient{}, AdGuardSeenClient{}},
	runtime.ResourceKindAdGuardDNS:                   {AdGuardDNSRecord{}},
	runtime.ResourceKindAdGuardQuerySummary:          {AdGuardQuerySummary{}},
	runtime.ResourceKindCaddyRoute:                   {CaddyRouteRecord{}},
	runtime.ResourceKindCloudflareZone:               {CloudflareZoneRecord{}},
	runtime.ResourceKindCloudflareDNSRecord:          {CloudflareDNSRecordStatus{}},
	runtime.ResourceKindCloudflarePagesProject:       {CloudflarePagesProjectRecord{}},
	runtime.ResourceKindCloudflareD1Database:         {CloudflareD1DatabaseRecord{}},
	runtime.ResourceKindCloudflareContactSubmission:  {runtime.CloudflareContactSubmissionRecord{}},
	runtime.ResourceKindCloudflareAccessApp:          {CloudflareAccessAppRecord{}},
	runtime.ResourceKindCloudflareAccessServiceToken: {CloudflareServiceTokenRecord{}},
	runtime.ResourceKindCloudflareTunnel:             {CloudflareTunnelRecord{}},
	runtime.ResourceKindCloudflareEdgeCertificate:    {CloudflareEdgeCertificateRecord{}},
	runtime.ResourceKindCloudflareRedirect:           {CloudflareRedirectRecord{}},
	runtime.ResourceKindGitHubRepository:             {GitHubRepositoryRecord{}, githubCarried{}},
	runtime.ResourceKindGitHubAllowance:              {runtime.GitHubAllowanceRecord{}},
	runtime.ResourceKindGitHubDelivery:               {GitHubDeliveryRecord{}},
	runtime.ResourceKindGitHubDependencyRun:          {GitHubDependencyRunRecord{}},
	runtime.ResourceKindGitHubProfile:                {GitHubProfileRecord{}},
	runtime.ResourceKindHostFirewall:                 {HostFirewallRecord{}},
	runtime.ResourceKindHostPerimeter:                {HostPerimeterRecord{}},
	runtime.ResourceKindHostRenderStatus:             {HostRenderStatusRecord{}},
	runtime.ResourceKindHostUnit:                     {runtime.HostUnitRecord{}},
	runtime.ResourceKindHostTailnetLock:              {runtime.HostTailnetLockRecord{}},
	runtime.ResourceKindNPMProxyHost:                 {NPMProxyHostRecord{}},
	runtime.ResourceKindNPMCertificate:               {NPMCertificateRecord{}},
	runtime.ResourceKindNPMRedirect:                  {NPMRedirectRecord{}},
	runtime.ResourceKindNPMDeadHost:                  {NPMDeadHostRecord{}},
	runtime.ResourceKindNPMStream:                    {NPMStreamRecord{}},
	runtime.ResourceKindNPMAccessList:                {NPMAccessListRecord{}},
	runtime.ResourceKindPortainerContainer:           {PortainerContainerRecord{}},
	runtime.ResourceKindPortainerEnvironment:         {PortainerEnvironmentRecord{}},
	runtime.ResourceKindPortainerNetwork:             {PortainerNetworkRecord{}},
	runtime.ResourceKindPortainerVolume:              {PortainerVolumeRecord{}},
	runtime.ResourceKindPortainerImage:               {PortainerImageRecord{}},
	runtime.ResourceKindPortainerRuntime:             {PortainerRuntimeRecord{}},
	runtime.ResourceKindPortainerComposeProject:      {PortainerComposeProject{}},
	runtime.ResourceKindTailscaleDevice:              {TailscaleDeviceRecord{}},
	runtime.ResourceKindTailscalePolicy:              {TailscalePolicyRecord{}},
	runtime.ResourceKindTailscaleDNS:                 {TailscaleDNSRecord{}},
	runtime.ResourceKindTailscaleSettings:            {TailscaleSettingsRecord{}},
	runtime.ResourceKindTailscaleUser:                {TailscaleUserRecord{}},
}

// filled is a value of the type with every member it can hold set: each
// pointer allocated, each list and map one entry long, each scalar non-zero.
func filled(t reflect.Type, depth int) reflect.Value {
	value := reflect.New(t).Elem()
	if depth > 8 {
		return value
	}
	switch {
	case t == reflect.TypeFor[json.RawMessage]():
		value.SetBytes([]byte(`"example"`))
		return value
	case t == reflect.TypeFor[time.Time]():
		value.Set(reflect.ValueOf(time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC)))
		return value
	}
	switch t.Kind() {
	case reflect.String:
		value.SetString("example")
	case reflect.Bool:
		value.SetBool(true)
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		value.SetInt(1)
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		value.SetUint(1)
	case reflect.Float32, reflect.Float64:
		value.SetFloat(1)
	case reflect.Interface:
		value.Set(reflect.ValueOf("example"))
	case reflect.Pointer:
		inner := reflect.New(t.Elem())
		inner.Elem().Set(filled(t.Elem(), depth+1))
		value.Set(inner)
	case reflect.Slice:
		value.Set(reflect.Append(reflect.MakeSlice(t, 0, 1), filled(t.Elem(), depth+1)))
	case reflect.Array:
		for i := range t.Len() {
			value.Index(i).Set(filled(t.Elem(), depth+1))
		}
	case reflect.Map:
		value.Set(reflect.MakeMap(t))
		value.SetMapIndex(filled(t.Key(), depth+1), filled(t.Elem(), depth+1))
	case reflect.Struct:
		for i := range t.NumField() {
			if t.Field(i).IsExported() {
				value.Field(i).Set(filled(t.Field(i).Type, depth+1))
			}
		}
	}
	return value
}

// Every reader's record types are named here, and the golden file is each
// of them filled as the controller writes it.
func TestRecordsGoldenHoldsEveryReadersRecords(t *testing.T) {
	readers := New(runtime.Environment{}, supplied(), &fakeHTTP{}).Coverage().Readers
	named := []string{}
	for kind := range recordTypes {
		named = append(named, string(kind))
	}
	slices.Sort(named)
	if !slices.Equal(named, readers) {
		t.Fatalf("recordTypes names %v\nthe readers are %v\nName each reader's record types in recordTypes.", named, readers)
	}
	records := map[runtime.ResourceKind][]any{}
	for kind, types := range recordTypes {
		for _, record := range types {
			records[kind] = append(records[kind], filled(reflect.TypeOf(record), 0).Interface())
		}
	}
	written, err := json.MarshalIndent(records, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	written = append(written, '\n')
	if *updateGolden {
		if err := os.WriteFile(recordsGolden, written, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	held, err := os.ReadFile(recordsGolden)
	if err != nil || !bytes.Equal(held, written) {
		t.Fatalf("%s is not every reader's records as the controller writes them. Run: go test ./providers -run TestRecordsGoldenHoldsEveryReadersRecords -update (in controller/)", recordsGolden)
	}
}
