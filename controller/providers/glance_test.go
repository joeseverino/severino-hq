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
	"sync"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Each container is sampled twice and its CPU is the share between the two;
// a one-shot sample's counter alone, all the CPU since the container started,
// never reads as current.
func TestEnvironmentGlanceMeasuresRunningContainers(t *testing.T) {
	docker := "/api/endpoints/1/docker"
	api, server := newPortainerAPI(t, map[string]string{
		"/api/endpoints":                      oneLocalEndpoint,
		docker + "/info":                      `{"NCPU":4,"MemTotal":8589934592}`,
		docker + glanceDiskUsage:              `{"LayersSize":1073741824,"Volumes":[{"UsageData":{"Size":1073741824}},{"UsageData":{"Size":-1}},{}],"BuildCache":[{"Size":1073741824}]}`,
		docker + "/containers/json?all=false": `[{"Id":"a"},{"Id":"b"}]`,
	})
	fixture := "docker-stats-one-shot-cgroup2.json"
	api.turns = map[string][]string{
		// A core and a half for 200ms, after a lifetime of heavy use.
		docker + oneShotStats("a"): {
			statsFixture(t, fixture, "2026-10-31T12:00:00Z", 900_000_000_000),
			statsFixture(t, fixture, "2026-10-31T12:00:00.2Z", 900_300_000_000),
		},
		// Half a core.
		docker + oneShotStats("b"): {
			statsFixture(t, fixture, "2026-10-31T12:00:00Z", 5_000_000_000),
			statsFixture(t, fixture, "2026-10-31T12:00:00.2Z", 5_100_000_000),
		},
	}
	r := portainerRegistry(t, server.URL, runtime.Environment{})
	machines, err := r.portainerGlance(t.Context(), map[string]bool{"hq-node": true})
	if err != nil || len(machines) != 1 {
		t.Fatalf("%+v %v", machines, err)
	}
	// Two cores busy of four is half the machine; the storage is the layers,
	// the measured volume and the build cache.
	read := machines[0].Docker
	if machines[0].Key != "hq-node" || read == nil || machines[0].Host != nil {
		t.Fatalf("%+v", machines[0])
	}
	if read.Containers != 2 || read.Cores != 4 || read.CPUPercent != 50 || read.MemoryTotal != 8589934592 ||
		read.MemoryUsed <= 0 || read.Storage != 3*1073741824 {
		t.Errorf("%+v", *read)
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
	machine := observations[0].Machines[0]
	if machine != (runtime.GlanceObservationMachine{Key: "hq-node", RefreshFailed: "credential"}) {
		t.Fatalf("%+v", machine)
	}
}

func hostGlanceRegistry(t *testing.T, stdout string) *Registry {
	t.Helper()
	env := runtime.Environment{SSHDir: t.TempDir()}
	held := supplied(sshConnection("srv", connections.SSHTransport{Host: "192.0.2.5", User: "hq", Port: "22", HostKey: "ssh-ed25519 AAAA"}))
	r := New(env, held, &fakeHTTP{})
	r.Commands = &Commands{Env: env, Supplied: held, Remote: func(context.Context, string, string, []byte) ([]byte, []byte, int, error) {
		return []byte(stdout), nil, 0, nil
	}}
	return r
}

func TestHostGlanceReadsTheTypedReading(t *testing.T) {
	r := hostGlanceRegistry(t, `{"cpu_percent":12.6,"cores":8,"load_1m":0.42,"memory_used":4294967296,"memory_total":17179869184,"storage_used":53687091200,"storage_total":107374182400}`)
	machine, err := r.hostGlance(t.Context(), "srv-box", "srv")
	want := runtime.GlanceObservationMachine{Key: "srv-box", Host: &runtime.GlanceObservationHost{
		CPUPercent: 12.6, Cores: 8, Load1M: 0.42, MemoryUsed: 4294967296, MemoryTotal: 17179869184,
		StorageUsed: 53687091200, StorageTotal: 107374182400,
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
		"/alerts/active?point=40.0000,-75.0000": `{"features":[{"properties":{"event":"Heat Advisory","headline":"Hot","severity":"Moderate"}},{"properties":{"event":"Other"}}]}`,
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
	if panel.City != "Example" || panel.State != "PA" || panel.Point != "40.0000,-75.0000" || len(panel.Hours) != 3 {
		t.Fatalf("%+v", panel)
	}
	first := panel.Hours[0]
	if first.Name != "This Afternoon" || *first.Temperature != 88 || first.TemperatureUnit != "F" || first.WindSpeed != "5 mph" ||
		first.WindDirection != "SW" || first.Forecast != "Sunny" || first.Chance != 10 {
		t.Errorf("%+v", first)
	}
	// The hour is the instant the Weather Service names, in its own offset.
	if start := panel.Hours[1].Start; !start.Equal(time.Date(2026, 7, 1, 18, 0, 0, 0, time.UTC)) {
		t.Errorf("%v", start)
	}
	if last := panel.Hours[2]; *last.Temperature != 70.5 || last.Chance != 0 {
		t.Errorf("%+v", last)
	}
	want := []runtime.GlanceObservationAlert{{Event: "Heat Advisory", Headline: "Hot", Severity: "Moderate"}, {Event: "Other"}}
	if !reflect.DeepEqual(panel.Alerts, want) {
		t.Errorf("%+v", panel.Alerts)
	}

	for point, want := range map[string]string{
		"40":         "the weather location must be latitude,longitude",
		"north,west": "the weather location is not numeric",
		"91,0":       "the weather location is outside valid coordinates",
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

// glanceSite is a fake Portainer API with a local environment of three running
// containers and an agent environment, and a fake weather service, behind one
// test server; edge-vps is also reachable over SSH.
func glanceSite(t *testing.T) (*Registry, runtime.GlancePlan) {
	t.Helper()
	docker := "/api/endpoints/1/docker"
	stats := statsFixture(t, "docker-stats-one-shot-cgroup2.json", "", 0)
	api, server := newPortainerAPI(t, map[string]string{
		"/api/endpoints": `[{"Id":1,"Name":"local","URL":"unix:///var/run/docker.sock","Type":1,"Status":1},
			{"Id":2,"Name":"edge-vps","URL":"tcp://192.0.2.20:9001","Type":2,"Status":1}]`,
		docker + "/info":                        `{"NCPU":2,"MemTotal":4294967296}`,
		docker + glanceDiskUsage:                `{"LayersSize":1073741824,"Volumes":[],"BuildCache":[]}`,
		docker + "/containers/json?all=false":   `[{"Id":"a"},{"Id":"b"},{"Id":"c"}]`,
		docker + oneShotStats("a"):              stats,
		docker + oneShotStats("b"):              stats,
		docker + oneShotStats("c"):              stats,
		"/points/40.0000,-75.0000":              `{"properties":{"forecastHourly":"SERVER/hourly","relativeLocation":{"properties":{"city":"Example","state":"PA"}}}}`,
		"/hourly":                               `{"properties":{"periods":[{"startTime":"2026-07-01T13:00:00-04:00","temperature":70,"shortForecast":"Sunny"}]}}`,
		"/alerts/active?point=40.0000,-75.0000": `{"features":[]}`,
	})
	api.routes["/points/40.0000,-75.0000"] = strings.Replace(api.routes["/points/40.0000,-75.0000"], "SERVER", server.URL, 1)
	env := runtime.Environment{ID: "hq-node", SSHDir: t.TempDir()}
	held := supplied(apiTokenConnection(runtime.ConnectionProviderPortainer, "portainer-example", server.URL, "synthetic"),
		sshConnection("edge", connections.SSHTransport{Host: "192.0.2.20", User: "hq", Port: "22", HostKey: "ssh-ed25519 AAAA"}))
	r := New(env, held, keyed{mustClient(t)})
	r.Resolve = func(context.Context, string) (string, error) { return "192.0.2.100", nil }
	r.Commands = &Commands{Env: env, Supplied: held, Remote: func(context.Context, string, string, []byte) ([]byte, []byte, int, error) {
		return []byte(`{"cpu_percent":5,"cores":2,"load_1m":0.1,"memory_used":1,"memory_total":2,"storage_used":1,"storage_total":2}`), nil, 0, nil
	}}
	plan := runtime.GlancePlan{
		Panels: []runtime.GlancePanelID{runtime.GlancePanelIDInfrastructure, runtime.GlancePanelIDWeather},
		Targets: runtime.GlancePlanTargets{
			Infrastructure: []runtime.GlanceMachineTarget{{Key: "edge-vps", Connections: []string{"edge"}}, {Key: "hq-node"}},
			Weather:        runtime.GlanceWeatherTarget{Point: "40,-75", Endpoint: server.URL},
		},
	}
	return r, plan
}

// glanceMachines is how each machine in the infrastructure panel was read.
func glanceMachines(t *testing.T, observations runtime.GlanceObservations) map[string]string {
	t.Helper()
	for _, observation := range observations {
		if observation.PanelID != runtime.GlancePanelIDInfrastructure {
			continue
		}
		read := map[string]string{}
		for _, machine := range observation.Machines {
			switch {
			case machine.Host != nil:
				read[machine.Key] = "host"
			case machine.Docker != nil:
				read[machine.Key] = "docker"
			default:
				read[machine.Key] = "unread " + machine.RefreshFailed
			}
		}
		return read
	}
	t.Fatal("no infrastructure panel")
	return nil
}

// A machine read over SSH is not read through Portainer as well: the glance
// reads only the environments that are machines it has no other way to read.
// The test server refuses a GET it has no route for, and edge-vps's
// environment has none.
func TestGlanceReadsOnlyTheEnvironmentsItNeeds(t *testing.T) {
	r, plan := glanceSite(t)
	observations, err := r.Glance(t.Context(), plan)
	if err != nil || len(observations) != 2 {
		t.Fatalf("%v %v", observations, err)
	}
	got := glanceMachines(t, observations)
	want := map[string]string{"edge-vps": "host", "hq-node": "docker"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

// meeting holds every call it gates until all of them are in flight together.
type meeting struct {
	mu      sync.Mutex
	want    int
	arrived int
	all     chan struct{}
}

func (m *meeting) wait(ctx context.Context) error {
	m.mu.Lock()
	m.arrived++
	if m.arrived == m.want {
		close(m.all)
	}
	m.mu.Unlock()
	select {
	case <-m.all:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

type gated struct {
	Transport
	meeting *meeting
	gate    func(address string) bool
}

func (g gated) Request(ctx context.Context, address, method string, headers map[string]string, payload any) (json.RawMessage, error) {
	if g.gate(address) {
		if err := g.meeting.wait(ctx); err != nil {
			return nil, err
		}
	}
	return g.Transport.Request(ctx, address, method, headers, payload)
}

// The glance reads its panels, its machines and a machine's containers at
// once: the three container samples, the SSH reading and the weather point
// are held until all five are in flight, which reads one at a time never are.
func TestGlanceReadsAtOnce(t *testing.T) {
	r, plan := glanceSite(t)
	m := &meeting{want: 5, all: make(chan struct{})}
	r.HTTP = gated{Transport: r.HTTP, meeting: m, gate: func(address string) bool {
		return strings.Contains(address, "/stats?") || strings.Contains(address, "/points/")
	}}
	reading := r.Commands.Remote
	r.Commands.Remote = func(ctx context.Context, ref, operation string, input []byte) ([]byte, []byte, int, error) {
		if err := m.wait(ctx); err != nil {
			return nil, nil, 0, err
		}
		return reading(ctx, ref, operation, input)
	}
	// Reads one at a time would wait for the meeting for good; the deadline
	// turns that into failed panels.
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	observations, err := r.Glance(ctx, plan)
	if err != nil {
		t.Fatal(err)
	}
	for _, observation := range observations {
		if observation.Weather != nil && observation.Weather.RefreshFailed != "" {
			t.Fatalf("weather was not read with the rest: %+v", observation.Weather)
		}
	}
	got := glanceMachines(t, observations)
	if got["hq-node"] != "docker" || got["edge-vps"] != "host" {
		t.Fatalf("machines were not read with the rest: %v", got)
	}
}

func TestHostFirewallReadingIsTyped(t *testing.T) {
	cases := []struct {
		name, body, err string
	}{
		{"the launcher's record", `{"record":"interface-binding","interface":"tailscale0","accept_requires_interface":true,"foreign_interface_dropped":false,"read_at":"2026-01-01T00:00:00Z"}`, ""},
		{"a field HQ does not know", `{"record":"interface-binding","tailnet_only":true}`, "decode host firewall reading"},
		{"a field of the wrong type", `{"record":"interface-binding","accept_requires_interface":"yes"}`, "decode host firewall reading"},
		{"no record", `{"interface":"tailscale0"}`, "host firewall reading names no record"},
		{"a name given twice", `{"record":"interface-binding","accept_requires_interface":false,"accept_requires_interface":true}`, "decode host firewall reading"},
		{"a name in another case", `{"record":"interface-binding","Accept_Requires_Interface":true}`, "decode host firewall reading"},
		{"trailing data", `{"record":"interface-binding"}]`, "decode host firewall reading"},
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
			r.Commands = &Commands{Env: env, Supplied: held, Remote: func(context.Context, string, string, []byte) ([]byte, []byte, int, error) {
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
