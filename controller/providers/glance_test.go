package providers

import (
	"context"
	"encoding/json"
	"errors"
	"maps"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

func TestEnvironmentGlanceMeasuresRunningContainers(t *testing.T) {
	docker := "/api/endpoints/1/docker"
	_, server := newPortainerAPI(t, map[string]string{
		"/api/endpoints":                      oneLocalEndpoint,
		docker + "/info":                      `{"NCPU":4,"MemTotal":8589934592}`,
		docker + "/system/df":                 `{"LayersSize":1073741824,"Volumes":[{"UsageData":{"Size":1073741824}},{"UsageData":{"Size":-1}},{}],"BuildCache":[{"Size":1073741824}]}`,
		docker + "/containers/json?all=false": `[{"Id":"a"},{"Id":"b"}]`,
		docker + "/containers/a/stats?stream=false&one-shot=true": `{"memory_stats":{"usage":1073741824,"stats":{"inactive_file":536870912}},
			"cpu_stats":{"cpu_usage":{"total_usage":300},"system_cpu_usage":1000,"online_cpus":4},
			"precpu_stats":{"cpu_usage":{"total_usage":100},"system_cpu_usage":600}}`,
		docker + "/containers/b/stats?stream=false&one-shot=true": `{"memory_stats":{"usage":536870912},
			"cpu_stats":{"cpu_usage":{"total_usage":50,"percpu_usage":[1,2]},"system_cpu_usage":100},
			"precpu_stats":{"cpu_usage":{"total_usage":50},"system_cpu_usage":0}}`,
	})
	r := portainerRegistry(t, server.URL, runtime.Environment{})
	machines, err := r.portainerGlance(t.Context())
	if err != nil || len(machines) != 1 {
		t.Fatalf("%+v %v", machines, err)
	}
	want := GlanceMachine{Key: "hq-node", Status: "good", Summary: "4 cores · 8.0 GB memory", Metrics: []GlanceMetric{
		{Label: "Containers", Value: "2", Detail: "running"},
		{Label: "CPU", Value: "50%", Detail: "of 4 cores"},
		{Label: "Memory", Value: "12%", Detail: "1.0 GB of 8.0 GB"},
		{Label: "Docker storage", Value: "3.0 GB", Detail: "layers, volumes and build cache"},
	}}
	if !reflect.DeepEqual(machines[0], want) {
		t.Errorf("got  %+v\nwant %+v", machines[0], want)
	}
}

func TestContainerCPUPercent(t *testing.T) {
	sample := func(total, previous, system, previousSystem uint64, online int, percpu int) dockerStats {
		var s dockerStats
		s.CPUStats.CPUUsage.TotalUsage, s.PreCPUStats.CPUUsage.TotalUsage = total, previous
		s.CPUStats.SystemUsage, s.PreCPUStats.SystemUsage = system, previousSystem
		s.CPUStats.OnlineCPUs = online
		s.CPUStats.CPUUsage.PercpuUsage = make([]uint64, percpu)
		return s
	}
	cases := []struct {
		name  string
		stats dockerStats
		want  float64
	}{
		{"online cpus", sample(300, 100, 1000, 600, 4, 0), 200},
		{"per-cpu list when online is absent", sample(150, 50, 400, 0, 0, 2), 50},
		{"one core when neither says", sample(150, 50, 400, 0, 0, 0), 25},
		{"no progress is idle", sample(100, 100, 1000, 600, 4, 0), 0},
		{"counter reset is idle", sample(50, 100, 1000, 600, 4, 0), 0},
	}
	for _, c := range cases {
		if got := containerCPUPercent(c.stats); got != c.want {
			t.Errorf("%s: %v, want %v", c.name, got, c.want)
		}
	}
}

// A failed panel is marked with the failure's contract class, so HQ keeps the
// last good reading and says why this one is missing.
func TestGlanceFailedPanelCarriesItsClass(t *testing.T) {
	cases := []struct {
		err  error
		want string
	}{
		{&ProviderError{Failure: runtime.FailureClassNetwork}, "network"},
		{&ProviderError{Failure: runtime.FailureClassCredential}, "credential"},
		{errors.New("decode"), "unclassified"},
	}
	for _, c := range cases {
		if got := glanceFailed(c.err); got != c.want {
			t.Errorf("%v: %q", c.err, got)
		}
	}

	api, server := newPortainerAPI(t, map[string]string{})
	api.status["/api/endpoints"] = 401
	r := portainerRegistry(t, server.URL, runtime.Environment{})
	plan := runtime.GlancePlan{Panels: []runtime.GlancePanelID{runtime.GlancePanelIDInfrastructure},
		Targets: runtime.GlancePlanTargets{Infrastructure: []runtime.GlanceMachineTarget{{Key: "hq-node"}}}}
	observations, err := r.Glance(t.Context(), plan)
	if err != nil || len(observations) != 1 {
		t.Fatalf("%v %v", observations, err)
	}
	machine := observations[0]["machines"].([]any)[0].(map[string]any)
	if machine["refresh_failed"] != "credential" || machine["summary"] != "Refresh failed (credential)." || machine["key"] != "hq-node" {
		t.Fatalf("%v", machine)
	}
}

func hostGlanceRegistry(t *testing.T, stdout string) *Registry {
	t.Helper()
	env := runtime.Environment{SSHDir: t.TempDir()}
	held := supplied(sshConnection("srv", connections.SSHTransport{Host: "192.0.2.5", User: "hq", Port: "22", HostKey: "ssh-ed25519 AAAA"}))
	r := New(env, held, &fakeHTTP{})
	r.Commands = &Commands{Env: env, Supplied: held, Exec: func(context.Context, []string, []byte, []string) ([]byte, []byte, int, error) {
		return []byte(stdout), nil, 0, nil
	}}
	return r
}

func TestHostGlanceReadsTheTypedReading(t *testing.T) {
	r := hostGlanceRegistry(t, `{"cpu_percent":12.6,"cores":8,"load_1m":0.42,"memory_used":4294967296,"memory_total":17179869184,"storage_used":53687091200,"storage_total":107374182400}`)
	machine, err := r.hostGlance(t.Context(), "srv-box", "srv")
	want := GlanceMachine{Key: "srv-box", Status: "good", Summary: "Host load 0.42", Metrics: []GlanceMetric{
		{Label: "CPU", Value: "13%", Detail: "8 cores"},
		{Label: "Memory", Value: "25%", Detail: "4.0 GB of 16.0 GB used"},
		{Label: "Storage", Value: "50%", Detail: "50.0 GB of 100.0 GB used on /"},
	}}
	if err != nil || !reflect.DeepEqual(machine, want) {
		t.Fatalf("got  %+v %v\nwant %+v", machine, err, want)
	}

	for _, bad := range []string{`not json`, `[]`, `{"cores":"eight"}`} {
		if _, err := hostGlanceRegistry(t, bad).hostGlance(t.Context(), "srv-box", "srv"); err == nil || !strings.HasPrefix(err.Error(), "host reading from srv: ") {
			t.Errorf("%s: %v", bad, err)
		}
	}
}

func TestWeatherGlance(t *testing.T) {
	api, server := newPortainerAPI(t, nil)
	api.routes = map[string]string{
		"/points/40.0000,-75.0000": `{"properties":{"forecastHourly":"` + "SERVER" + `/gridpoints/PHI/1,1/forecast/hourly",
			"relativeLocation":{"properties":{"city":"Example","state":"PA"}}}}`,
		"/alerts/active?point=40.0000,-75.0000": `{"features":[{"properties":{"event":"Heat Advisory","headline":"Hot"}},{"properties":{"event":"Other"}}]}`,
	}
	forecast := `{"properties":{"periods":[
		{"name":"This Afternoon","startTime":"2026-07-01T13:00:00-04:00","temperature":88,"temperatureUnit":"F","windSpeed":"5 mph","windDirection":"SW","shortForecast":"Sunny","probabilityOfPrecipitation":{"value":10}},
		{"startTime":"2026-07-01T14:00:00-04:00","temperature":91,"shortForecast":"Storms","probabilityOfPrecipitation":{"value":60}},
		{"startTime":"2026-07-01T00:00:00-04:00","temperature":70.5,"probabilityOfPrecipitation":{"value":null}}]}}`
	api.routes["/gridpoints/PHI/1,1/forecast/hourly"] = forecast
	api.routes["/points/40.0000,-75.0000"] = strings.Replace(api.routes["/points/40.0000,-75.0000"], "SERVER", server.URL, 1)
	r := New(runtime.Environment{}, supplied(), mustClient(t))
	// NWS takes no API key; the test server asks for one, so send it.
	r.HTTP = keyed{r.HTTP}
	panel, err := r.weatherGlance(t.Context(), runtime.GlanceWeatherTarget{Point: " 40, -75 ", Endpoint: server.URL})
	if err != nil {
		t.Fatal(err)
	}
	want := []GlanceMetric{
		{Label: "Now", Value: "Sunny", Detail: "This Afternoon"},
		{Label: "Temperature", Value: "88°F"},
		{Label: "Range", Value: "70.5–91°", Detail: "next 3 hours"},
		{Label: "Rain", Value: "2 PM · 60%", Detail: "Storms"},
		{Label: "Wind", Value: "SW 5 mph", Detail: "NWS hourly forecast"},
		{Label: "Alerts", Value: "Heat Advisory +1", Detail: "Hot"},
	}
	if panel.Status != "serious" || panel.Summary != "Example, PA" || panel.Point != "40.0000,-75.0000" || !reflect.DeepEqual(panel.Metrics, want) {
		t.Fatalf("%+v", panel)
	}
	if got := panel.Hours[2]; got != (GlanceHour{Time: "12 AM", Temperature: "70.5°", Precipitation: "0%"}) {
		t.Errorf("%+v", got)
	}

	for point, want := range map[string]string{
		"40":         "SEVERINO_NWS_POINT must be latitude,longitude",
		"north,west": "SEVERINO_NWS_POINT is not numeric",
		"91,0":       "SEVERINO_NWS_POINT is outside valid coordinates",
	} {
		if _, err := r.weatherGlance(t.Context(), runtime.GlanceWeatherTarget{Point: point, Endpoint: server.URL}); err == nil || err.Error() != want {
			t.Errorf("%s: %v", point, err)
		}
	}
	if _, err := r.weatherGlance(t.Context(), runtime.GlanceWeatherTarget{Point: "40,-75"}); err == nil {
		t.Error("a plan naming no weather API was read")
	}
}

func mustClient(t *testing.T) *runtime.HTTPClient {
	t.Helper()
	client, err := runtime.NewHTTPClient("")
	if err != nil {
		t.Fatal(err)
	}
	return client
}

// keyed adds the test server's key to every request.
type keyed struct{ Transport }

func (k keyed) Request(ctx context.Context, address, method string, headers map[string]string, payload any) (json.RawMessage, error) {
	with := map[string]string{"X-API-Key": "synthetic"}
	maps.Copy(with, headers)
	return k.Transport.Request(ctx, address, method, with, payload)
}

func TestHostFirewallReadingIsTyped(t *testing.T) {
	cases := []struct {
		name, body, err string
	}{
		{"the launcher's record", `{"record":"interface-binding","interface":"tailscale0","accept_requires_interface":true,"foreign_interface_dropped":false,"read_at":"2026-01-01T00:00:00Z"}`, ""},
		{"a field HQ does not know", `{"record":"interface-binding","tailnet_only":true}`, "decode host firewall reading"},
		{"a field of the wrong type", `{"record":"interface-binding","accept_requires_interface":"yes"}`, "decode host firewall reading"},
		{"no record", `{"interface":"tailscale0"}`, "host firewall reading names no record"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "firewall.json")
			if err := os.WriteFile(path, []byte(c.body), 0o600); err != nil {
				t.Fatal(err)
			}
			records, err := New(runtime.Environment{HostFirewall: path}, supplied(), &fakeHTTP{}).hostFirewall(t.Context())
			if c.err == "" {
				want := HostFirewallRecord{Record: "interface-binding", Interface: "tailscale0", AcceptRequiresInterface: true, ReadAt: "2026-01-01T00:00:00Z"}
				if err != nil || !reflect.DeepEqual(records, []any{want}) {
					t.Fatalf("%v %v", records, err)
				}
				return
			}
			if err == nil || !strings.HasPrefix(err.Error(), c.err) {
				t.Fatalf("got %v", err)
			}
		})
	}
	if _, err := New(runtime.Environment{}, supplied(), &fakeHTTP{}).hostFirewall(t.Context()); err == nil {
		t.Error("no mounted reading is an error")
	}
}

func TestHostPerimeterChecksPublishedPortsAndSSH(t *testing.T) {
	env := runtime.Environment{SSHDir: t.TempDir()}
	held := supplied(sshConnection("edge", connections.SSHTransport{Host: "192.0.2.9", User: "hq", Port: "7722", Role: "caddy", HostKey: "ssh-ed25519 AAAA"}))
	cases := []struct {
		name, stdout, unit string
		addresses          []string
		err                bool
	}{
		{"a reading", `{"public_addresses":"203.0.113.5, 203.0.113.6","firewall_unit":"active","read_at":"2026-01-01T00:00:00Z"}`, "active", []string{"203.0.113.5", "203.0.113.6"}, false},
		{"an empty answer", ``, "unknown", []string{}, false},
		{"not a reading", `["x"]`, "", nil, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := New(env, held, &fakeHTTP{})
			r.Commands = &Commands{Env: env, Supplied: held, Exec: func(context.Context, []string, []byte, []string) ([]byte, []byte, int, error) {
				return []byte(c.stdout), nil, 0, nil
			}}
			r.PublishedContainers = func(context.Context) ([]PublishedContainer, error) {
				return []PublishedContainer{{Host: "edge", Ports: []int{443}}, {Host: "elsewhere", Ports: []int{9999}}}, nil
			}
			r.Dial = func(_ context.Context, address string, port int) bool { return address == "203.0.113.5" && port == 443 }
			records, err := r.hostPerimeter(t.Context())
			if c.err {
				if err == nil || !strings.HasPrefix(err.Error(), "SSH perimeter for edge: decode reading") {
					t.Fatalf("got %v", err)
				}
				return
			}
			if err != nil || len(records) != 1 {
				t.Fatalf("%v %v", records, err)
			}
			record := records[0].(HostPerimeterRecord)
			answered := []int{}
			if len(c.addresses) > 0 {
				answered = []int{443}
			}
			if record.FirewallUnit != c.unit || !reflect.DeepEqual(record.PublicAddresses, c.addresses) ||
				!reflect.DeepEqual(record.PortsChecked, []int{22, 443, 7722}) || !reflect.DeepEqual(record.AnsweredPublicly, answered) {
				t.Fatalf("%+v", record)
			}
		})
	}
}
