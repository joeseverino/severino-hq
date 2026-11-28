package providers

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/joeseverino/severino-hq/controller/host/unit"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// portainerAPI is Portainer behind a real HTTP server: GET answers by path and
// query, writes recorded, any route answerable with a status instead.
type portainerAPI struct {
	t      *testing.T
	mu     sync.Mutex
	routes map[string]string   // path?query -> JSON body
	status map[string]int      // path?query -> non-2xx status
	turns  map[string][]string // path?query -> bodies answered in turn, the last repeated
	writes []portainerWrite
}

type portainerWrite struct {
	method, path string
	body         map[string]any
}

func newPortainerAPI(t *testing.T, routes map[string]string) (*portainerAPI, *httptest.Server) {
	api := &portainerAPI{t: t, routes: routes, status: map[string]int{}}
	server := httptest.NewServer(api)
	t.Cleanup(server.Close)
	return api, server
}

func (a *portainerAPI) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	if req.Header.Get("X-API-Key") != "synthetic" {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	key := req.URL.Path
	if req.URL.RawQuery != "" {
		key += "?" + req.URL.RawQuery
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if code := a.status[key]; code != 0 {
		w.WriteHeader(code)
		return
	}
	if req.Method != http.MethodGet {
		var body map[string]any
		data, _ := io.ReadAll(req.Body)
		_ = json.Unmarshal(data, &body)
		a.writes = append(a.writes, portainerWrite{req.Method, key, body})
		w.WriteHeader(http.StatusNoContent)
		return
	}
	answer, ok := a.routes[key]
	if bodies := a.turns[key]; len(bodies) > 0 {
		answer, ok = bodies[0], true
		if len(bodies) > 1 {
			a.turns[key] = bodies[1:]
		}
	}
	if !ok {
		a.t.Errorf("unexpected GET %s", key)
		w.WriteHeader(http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = io.WriteString(w, answer)
}

func (a *portainerAPI) written() []portainerWrite {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]portainerWrite{}, a.writes...)
}

func portainerRegistry(t *testing.T, base string, extra runtime.Environment) *Registry {
	t.Helper()
	client, err := runtime.NewHTTPClient("")
	if err != nil {
		t.Fatal(err)
	}
	env := extra
	env.ID = "hq-node"
	r := New(env, supplied(apiTokenConnection(runtime.ConnectionProviderPortainer, "portainer-example", base, "synthetic")), client)
	r.Resolve = func(context.Context, string) (string, error) { return "192.0.2.100", nil }
	return r
}

const (
	oneLocalEndpoint = `[{"Id":1,"Name":"local","URL":"unix:///var/run/docker.sock","Type":1,"Status":1}]`
	containersPath   = "/api/endpoints/1/docker/containers/json?all=1"
)

func TestPortainerEnvironmentsAreTheirMachines(t *testing.T) {
	_, server := newPortainerAPI(t, map[string]string{"/api/endpoints": `[
		{"Id":1,"Name":"local","URL":"unix:///var/run/docker.sock","Type":1,"Status":1,"Agent":{"Version":"2.19.4"},
		 "Snapshots":[{"DockerVersion":"26.0"},{"DockerVersion":"27.1","RunningContainerCount":3,"ContainerCount":4,"Time":1767355200}]},
		{"Id":2,"Name":"edge-vps","URL":"tcp://192.0.2.20:9001","Type":2,"Status":1,"Snapshots":[]},
		{"Id":3,"Name":"odd","URL":"tcp://192.0.2.30:9001","Type":9,"Status":2}]`})
	r := portainerRegistry(t, server.URL, runtime.Environment{})
	found, err := r.portainerEnvironments(t.Context(), "portainer-example")
	if err != nil || len(found) != 3 {
		t.Fatalf("%+v %v", found, err)
	}
	want := []PortainerEnvironment{
		{ID: 1, Name: "local", Address: urlHostname(server.URL), Local: true, Reachable: true, Type: "docker", Status: "up", AgentVersion: "2.19.4",
			DockerVersion: "27.1", SnapshotAt: "2026-01-02T12:00:00Z"},
		{ID: 2, Name: "edge-vps", Address: "192.0.2.20", Reachable: true, Type: "agent", Status: "up"},
		{ID: 3, Name: "odd", Address: "192.0.2.30", Type: "9", Status: "down"},
	}
	if !reflect.DeepEqual(found, want) {
		t.Errorf("got  %+v\nwant %+v", found, want)
	}
}

// A field of the wrong JSON type is an error, never coerced into a value.
func TestPortainerMalformedAnswersAreErrors(t *testing.T) {
	cases := []struct {
		name, path, body string
		read             func(*Registry) error
	}{
		{"environment id as text", "/api/endpoints", `[{"Id":"one","Status":1}]`, func(r *Registry) error {
			_, err := r.portainerEnvironments(t.Context(), "portainer-example")
			return err
		}},
		{"snapshot that is not an object", "/api/endpoints", `[{"Id":1,"Status":1,"Snapshots":["not a snapshot"]}]`, func(r *Registry) error {
			_, err := r.portainerEnvironments(t.Context(), "portainer-example")
			return err
		}},
		{"public port as text", containersPath, `[{"Id":"a","Ports":[{"PublicPort":"8080"}]}]`, func(r *Registry) error {
			_, err := r.portainerContainers(t.Context(), "portainer-example", 1)
			return err
		}},
		{"label that is not text", containersPath, `[{"Id":"a","Labels":{"com.docker.compose.project":7}}]`, func(r *Registry) error {
			_, err := r.portainerContainers(t.Context(), "portainer-example", 1)
			return err
		}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, server := newPortainerAPI(t, map[string]string{c.path: c.body})
			err := c.read(portainerRegistry(t, server.URL, runtime.Environment{}))
			if err == nil || !strings.HasPrefix(err.Error(), "decode Portainer ") {
				t.Fatalf("got %v", err)
			}
		})
	}
}

func TestPortainerRefusalsClassify(t *testing.T) {
	cases := []struct {
		code    int
		refusal runtime.Refusal
		says    string
	}{
		{401, runtime.RefusalCredential, "environment list: credential refused"},
		{403, runtime.RefusalPermission, "environment list needs environment access"},
		{500, runtime.RefusalUnclassified, "environment list: the service answered 500"},
	}
	for _, c := range cases {
		api, server := newPortainerAPI(t, map[string]string{})
		api.status["/api/endpoints"] = c.code
		r := portainerRegistry(t, server.URL, runtime.Environment{})
		_, err := r.portainerEnvironmentReading(t.Context())
		if _, refusal, _ := runtime.Classify(err); refusal != c.refusal || !strings.HasPrefix(err.Error(), c.says) {
			t.Errorf("%d: %q %v", c.code, refusal, err)
		}
	}
}

// The sweep leaves out the containers of hq-host's runs in flight, a deploy's
// as well as its own, by the nonces root registered, so a container cannot
// hide by wearing the label with a value of its choosing. A run that ended
// between the list and the look at its nonce removed its container first, so
// one Docker no longer has is left out, and one it still has is reported.
func TestPortainerSweepSkipsOnlyHostRuns(t *testing.T) {
	const pass, rehearsal, ended = "AAAAAAAAAAAAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBBBBBBBBBBBB", "CCCCCCCCCCCCCCCCCCCCCCCCCC"
	byID := func(id string) string {
		return "/api/endpoints/1/docker/containers/json?all=1&filters=" + url.QueryEscape(`{"id":["`+id+`"]}`)
	}
	root := t.TempDir()
	runs := filepath.Join(root, "runs")
	for _, file := range []string{filepath.Join(runs, pass), filepath.Join(runs, rehearsal), filepath.Join(root, "beside")} {
		if err := os.MkdirAll(filepath.Dir(file), 0o711); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(file, nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	_, server := newPortainerAPI(t, map[string]string{
		"/api/endpoints": oneLocalEndpoint,
		"/api/stacks":    `[]`,
		containersPath: `[
			{"Id":"aaaaaaaaaaaaaaaa","Names":["/hq-controller"],"Labels":{"severino-hq.run":"` + pass + `"},"State":"running"},
			{"Id":"bbbbbbbbbbbbbbbb","Names":["/severino-hq-rehearsal"],"Labels":{"severino-hq.run":"` + rehearsal + `"},"State":"running"},
			{"Id":"dddddddddddddddd","Names":["/stranger"],"Labels":{"severino-hq.run":"` + ended + `"},"State":"running"},
			{"Id":"ffffffffffffffff","Names":["/severino-hq-automation-x"],"Labels":{"severino-hq.run":"` + ended + `"},"State":"running"},
			{"Id":"eeeeeeeeeeeeeeee","Names":["/climber"],"Labels":{"severino-hq.run":"../beside"},"State":"running"},
			{"Id":"cccccccccccccccc","Names":["/web"],"State":"running"}]`,
		byID("dddddddddddddddd"): `[{"Id":"dddddddddddddddd","Names":["/stranger"],"State":"running"}]`,
		byID("ffffffffffffffff"): `[]`,
	})
	r := portainerRegistry(t, server.URL, runtime.Environment{Runs: runs})
	records, err := r.portainerContainerRecords(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	names := []string{}
	for _, record := range records {
		names = append(names, record.Name)
	}
	if !reflect.DeepEqual(names, []string{"stranger", "climber", "web"}) {
		t.Fatalf("swept %v", names)
	}
}

func TestPortainerContainerRecord(t *testing.T) {
	yes := true
	container := dockerContainer{
		ID: "abcdef0123456789", Names: []string{"/web-app-1", "/alias"}, Image: "ghcr.io/example/web:1", State: "running",
		Labels: map[string]string{composeProject: "web", composeWorkingDir: "/srv/web", imageSourceLabel: "https://example.invalid/web"},
		Ports:  []dockerPort{{IP: "0.0.0.0", PublicPort: 8080}, {IP: "::", PublicPort: 8080}, {PrivatePort: 9999}},
		Mounts: []dockerMount{{Type: "volume", Name: "data", RW: &yes}},
	}
	record := containerRecord(container, "hq-node", "portainer-example", map[string]bool{"web": true}, "192.0.2.100")
	single := 8080
	want := PortainerContainerRecord{
		PortainerManaged: true, Name: "web-app-1", ID: "abcdef012345", Stack: "web", WorkingDir: "/srv/web",
		Image: "ghcr.io/example/web:1", Source: "https://example.invalid/web", State: "running", Ports: []int{8080}, Port: &single,
		Reachable: true, Host: "hq-node", HostAddress: "192.0.2.100", ConnectionRef: "portainer-example",
	}
	if !reflect.DeepEqual(record, want) {
		t.Fatalf("got  %+v\nwant %+v", record, want)
	}

	cases := []struct {
		name      string
		ports     []dockerPort
		listed    []int
		single    bool
		reachable bool
	}{
		{"nothing published", nil, []int{}, false, true},
		{"two ports, none single", []dockerPort{{PublicPort: 443}, {PublicPort: 80}}, []int{80, 443}, false, true},
		{"loopback hides it", []dockerPort{{IP: "127.0.0.1", PublicPort: 5432}}, []int{5432}, true, false},
		{"IPv6 loopback hides it", []dockerPort{{IP: "::1", PublicPort: 5432}}, []int{5432}, true, false},
	}
	for _, c := range cases {
		listed, port, reachable := dockerContainer{Ports: c.ports}.published()
		if !reflect.DeepEqual(listed, c.listed) || (port != nil) != c.single || reachable != c.reachable {
			t.Errorf("%s: %v %v %v", c.name, listed, port, reachable)
		}
	}
	if containerRecord(container, "", "", nil, "").PortainerManaged {
		t.Error("a stack Portainer did not create is not Portainer-managed")
	}
}

func TestPortainerRuntimeRecord(t *testing.T) {
	var inspect dockerInspect
	if err := json.Unmarshal([]byte(`{
		"Image":"sha256:img1",
		"Config":{"User":"1000:1000","Labels":{"com.docker.compose.project":"web","com.docker.compose.service":"app"},
		          "ExposedPorts":{"443/tcp":{},"80/tcp":{},"abc/udp":{}},"Healthcheck":{"Test":["CMD","true"]}},
		"HostConfig":{"ReadonlyRootfs":true,"NetworkMode":"web_default","CapAdd":["NET_ADMIN",""],"CapDrop":["ALL"],
		              "SecurityOpt":["no-new-privileges:true","seccomp={\"defaultAction\": \"SCMP_ACT_ERRNO\", \"architectures\": [\"SCMP_ARCH_X86_64\"], \"syscalls\": []}"],
		              "Devices":[{"PathOnHost":"/dev/fuse"}],
		              "PortBindings":{"80/tcp":[{"HostIp":"","HostPort":"8080"}],"22/tcp":null},
		              "Memory":536870912,"NanoCpus":1500000000,"PidsLimit":null,"RestartPolicy":{"Name":"unless-stopped"}},
		"State":{"Health":{"Status":"healthy"},"StartedAt":"2026-01-02T10:00:00Z"},
		"Mounts":[{"Type":"volume","Name":"web_data","Destination":"/data","RW":true},
		          {"Type":"bind","Source":"/srv/web/conf","Destination":"/conf","RW":false},
		          {"Type":"bind","Source":"/srv/silent","Destination":"/silent"}],
		"RestartCount":2,
		"Env":["SECRET=never copied"]}`), &inspect); err != nil {
		t.Fatal(err)
	}
	site := portainerSite{ConnectionRef: "portainer-example", EnvironmentID: 1, Host: "hq-node"}
	record := runtimeRecord(site, dockerContainer{Names: []string{"/web-app-1"}}, inspect)
	want := PortainerRuntimeRecord{
		portainerSite: site, Container: "web-app-1", Stack: "web", Service: "app", ImageID: "sha256:img1", User: "1000:1000",
		ReadOnlyRootfs: true, NetworkMode: "web_default", CapAdd: []string{"NET_ADMIN"}, CapDrop: []string{"ALL"},
		SecurityOpt: []string{"no-new-privileges:true", "seccomp=(profile)"}, Devices: []string{"/dev/fuse"},
		Mounts: []PortainerRuntimeMount{
			{Type: "volume", Source: "web_data", Destination: "/data"},
			{Type: "bind", Source: "/srv/web/conf", Destination: "/conf", ReadOnly: true},
			{Type: "bind", Source: "/srv/silent", Destination: "/silent"},
		},
		PortBindings: []PortainerPortBinding{{ContainerPort: "80/tcp", HostPort: "8080"}},
		ExposedPorts: []int{80, 443}, MemoryLimit: 536870912, CPULimit: 1.5, RestartPolicy: "unless-stopped",
		Healthcheck: true, Health: "healthy", RestartCount: 2, StartedAt: "2026-01-02T10:00:00Z",
	}
	if !reflect.DeepEqual(record, want) {
		t.Fatalf("got  %+v\nwant %+v", record, want)
	}
	if data, _ := json.Marshal(record); strings.Contains(string(data), "never copied") {
		t.Error("the inspect's environment reached the record")
	}

	cases := []struct {
		test    []string
		nano    int64
		checked bool
		cpu     float64
	}{
		{[]string{"NONE"}, 125000000, false, 0.13},
		{nil, 0, false, 0},
		{[]string{"CMD-SHELL", "curl -f localhost"}, 2000000000, true, 2},
	}
	for _, c := range cases {
		var doc dockerInspect
		doc.HostConfig.NanoCpus = c.nano
		if c.test != nil {
			doc.Config.Healthcheck = &struct {
				Test []string `json:"Test"`
			}{c.test}
		}
		got := runtimeRecord(site, dockerContainer{}, doc)
		if got.Healthcheck != c.checked || got.CPULimit != c.cpu {
			t.Errorf("%v %d: healthcheck %v cpu %v", c.test, c.nano, got.Healthcheck, got.CPULimit)
		}
	}
}

func TestPortainerSiteReadings(t *testing.T) {
	_, server := newPortainerAPI(t, map[string]string{
		"/api/endpoints": oneLocalEndpoint,
		"/api/stacks":    `[{"Id":10,"Name":"web","EndpointId":1,"Status":1,"EntryPoint":"docker-compose.yml"},{"Id":11,"Name":"orphan","EndpointId":1,"Status":2,"ProjectPath":"/data/compose/11"},{"Id":12,"Name":"web","EndpointId":2}]`,
		containersPath: `[
			{"Id":"a1","Names":["/web-app-1"],"Image":"web:1","ImageID":"sha256:img1",
			 "Labels":{"com.docker.compose.project":"web","com.docker.compose.service":"app","com.docker.compose.project.config_files":"/srv/web/compose.yml, /srv/web/override.yml"},
			 "NetworkSettings":{"Networks":{"web_default":{},"proxy":{}}},
			 "Mounts":[{"Type":"volume","Name":"web_data","Destination":"/data","RW":true},{"Type":"bind","Source":"/srv/web/conf","Destination":"/conf","RW":false}]},
			{"Id":"a2","Names":["/web-db-1"],"ImageID":"sha256:img2","Labels":{"com.docker.compose.project":"web"},
			 "NetworkSettings":{"Networks":{"web_default":{}}},
			 "Mounts":[{"Type":"bind","Source":"/srv/web/conf","Destination":"/etc/db","RW":true}]}]`,
		"/api/endpoints/1/docker/networks":    `[{"Id":"n1","Name":"web_default","Driver":"bridge","IPAM":{"Config":[{"Subnet":"172.20.0.0/16"},{"Gateway":"172.20.0.1"}]}},{"Id":"n2","Name":"proxy","Internal":true,"IPAM":{"Config":null}},{"Name":""}]`,
		"/api/endpoints/1/docker/volumes":     `{"Volumes":[{"Name":"web_data","Driver":"local","Labels":{"com.docker.compose.project":"web"}},{"Name":"unused"}]}`,
		"/api/endpoints/1/docker/images/json": `[{"Id":"sha256:img1","RepoTags":["web:1","<none>:<none>"],"RepoDigests":["web@sha256:d1","<none>@<none>"],"Created":1767355200,"Size":1234},{"Id":""}]`,
	})
	r := portainerRegistry(t, server.URL, runtime.Environment{})
	ctx := t.Context()
	read := func(kind runtime.ResourceKind) []map[string]any {
		t.Helper()
		records, err := r.readers[string(kind)](ctx)
		if err != nil {
			t.Fatalf("%s: %v", kind, err)
		}
		return asRecords(records)
	}

	networks := read(runtime.ResourceKindPortainerNetwork)
	if len(networks) != 2 || !reflect.DeepEqual(networks[0]["subnets"], []any{"172.20.0.0/16"}) ||
		!reflect.DeepEqual(networks[0]["containers"], []any{"web-app-1", "web-db-1"}) || networks[1]["internal"] != true {
		t.Errorf("networks %v", networks)
	}
	volumes := read(runtime.ResourceKindPortainerVolume)
	if len(volumes) != 3 || volumes[0]["stack"] != "web" || len(volumes[1]["used_by"].([]any)) != 0 ||
		volumes[2]["type"] != "bind" || len(volumes[2]["used_by"].([]any)) != 2 {
		t.Errorf("volumes %v", volumes)
	}
	images := read(runtime.ResourceKindPortainerImage)
	if len(images) != 1 || !reflect.DeepEqual(images[0]["tags"], []any{"web:1"}) || !reflect.DeepEqual(images[0]["digests"], []any{"web@sha256:d1"}) ||
		images[0]["created_at"] != "2026-01-02T12:00:00Z" || images[0]["size"] != float64(1234) {
		t.Errorf("images %v", images)
	}
	projects := read(runtime.ResourceKindPortainerComposeProject)
	if len(projects) != 2 {
		t.Fatalf("projects %v", projects)
	}
	orphan, web := projects[0], projects[1]
	if orphan["source"] != "portainer" || orphan["status"] != "inactive" || orphan["working_dir"] != "/data/compose/11" || orphan["config_files"] != nil {
		t.Errorf("orphan %v", orphan)
	}
	if web["source"] != "portainer" || web["status"] != "active" || web["entry_point"] != "docker-compose.yml" ||
		!reflect.DeepEqual(web["config_files"], []any{"/srv/web/compose.yml", "/srv/web/override.yml"}) ||
		!reflect.DeepEqual(web["containers"], []any{"web-app-1", "web-db-1"}) {
		t.Errorf("web %v", web)
	}
}

// One environment that cannot be read is that machine's refused part; every
// environment refusing is the reading refused.
func TestPortainerEachRefusesPerMachine(t *testing.T) {
	routes := map[string]string{
		"/api/endpoints":                   `[{"Id":1,"Name":"local","URL":"unix:///var/run/docker.sock","Status":1},{"Id":2,"Name":"edge","URL":"tcp://192.0.2.20:9001","Status":1}]`,
		containersPath:                     `[{"Id":"a1","Names":["/web"],"NetworkSettings":{"Networks":{"bridge":{}}}}]`,
		"/api/endpoints/1/docker/networks": `[{"Id":"n1","Name":"bridge"}]`,
	}
	api, server := newPortainerAPI(t, routes)
	api.status["/api/endpoints/2/docker/containers/json?all=1"] = 403
	r := portainerRegistry(t, server.URL, runtime.Environment{})
	ledger := &refusals{}
	ctx := context.WithValue(t.Context(), refusalKey{}, ledger)
	records, err := r.readers[string(runtime.ResourceKindPortainerNetwork)](ctx)
	if err != nil || len(records) != 1 {
		t.Fatalf("%v %v", records, err)
	}
	if len(ledger.entries) != 1 || ledger.entries[0].Scope != "edge" || ledger.entries[0].Address != "192.0.2.20" || ledger.entries[0].Refusal != runtime.FailureClassPermission {
		t.Fatalf("%+v", ledger.entries)
	}

	api.status[containersPath] = 403
	r = portainerRegistry(t, server.URL, runtime.Environment{})
	if _, err := r.readers[string(runtime.ResourceKindPortainerNetwork)](t.Context()); err == nil || err.Error() != "network list needs environment access: the service answered 403" {
		t.Fatalf("every machine refusing refuses the reading: %v", err)
	}
}

func TestPortainerCyclerReadsBackTheState(t *testing.T) {
	cases := []struct {
		verb, state, kind string
	}{
		{"stop", "exited", "Ready"},
		{"start", "exited", "Degraded"},
		{"restart", "running", "Ready"},
	}
	for _, c := range cases {
		api, server := newPortainerAPI(t, map[string]string{
			"/api/endpoints": oneLocalEndpoint,
			containersPath:   `[{"Id":"abc","Names":["/other"]},{"Id":"def","Names":["/web"],"State":"` + c.state + `"}]`,
		})
		r := portainerRegistry(t, server.URL, runtime.Environment{})
		res, err := r.portainerCycler(c.verb)(t.Context(), PortainerContainerSpec{ConnectionRef: "portainer-example", Host: "hq-node", Name: "web"}, struct{}{}, true)
		if err != nil || string(res.Conditions[0].Type) != c.kind || res.Message != "web is "+c.state+"." {
			t.Errorf("%s: %+v %v", c.verb, res, err)
		}
		if writes := api.written(); len(writes) != 1 || writes[0].path != "/api/endpoints/1/docker/containers/def/"+c.verb {
			t.Errorf("%s: %+v", c.verb, writes)
		}
	}
	_, server := newPortainerAPI(t, map[string]string{"/api/endpoints": oneLocalEndpoint, containersPath: `[]`})
	r := portainerRegistry(t, server.URL, runtime.Environment{})
	if _, err := r.portainerCycler("stop")(t.Context(), PortainerContainerSpec{ConnectionRef: "portainer-example", Host: "hq-node", Name: "web"}, struct{}{}, true); err == nil || err.Error() != `no container named "web" on hq-node` {
		t.Fatalf("got %v", err)
	}
	api, server := newPortainerAPI(t, map[string]string{"/api/endpoints": oneLocalEndpoint})
	r = portainerRegistry(t, server.URL, runtime.Environment{})
	if _, err := r.portainerCycler("stop")(t.Context(), PortainerContainerSpec{ConnectionRef: "portainer-example", Host: "elsewhere", Name: "web"}, struct{}{}, true); err == nil || err.Error() != `no Portainer environment is "elsewhere"` {
		t.Fatalf("got %v", err)
	}
	if len(api.written()) != 0 {
		t.Fatal("a refusal writes nothing")
	}
}

// units stands in for hq-host unit on the machines it is given.
type units struct {
	on   []string
	acts []string
}

func (u *units) Reaches(host string) bool { return slices.Contains(u.on, host) }

func (u *units) Act(_ context.Context, host, verb, name string) error {
	u.acts = append(u.acts, host+" "+verb+" "+name)
	return nil
}

// supervisedList is Docker's container list with web started by its unit.
const supervisedList = `[{"Id":"def","Names":["/web"],"State":"exited","Labels":{"com.docker.compose.project":"web","severino-hq.unit":"web.service"}}]`

func TestPortainerCyclerActsThroughTheUnitALabelNames(t *testing.T) {
	for _, verb := range []string{"stop", "start", "restart"} {
		api, server := newPortainerAPI(t, map[string]string{"/api/endpoints": oneLocalEndpoint, containersPath: supervisedList})
		r := portainerRegistry(t, server.URL, runtime.Environment{})
		control := &units{on: []string{"hq-node"}}
		r.Units = control
		res, err := r.portainerCycler(verb)(t.Context(), PortainerContainerSpec{ConnectionRef: "portainer-example", Host: "hq-node", Name: "web"}, struct{}{}, true)
		if err != nil || res.Message != "web is exited." {
			t.Fatalf("%s: %+v %v", verb, res, err)
		}
		if !slices.Equal(control.acts, []string{"hq-node " + verb + " web.service"}) {
			t.Errorf("%s: asked the unit %v", verb, control.acts)
		}
		if writes := api.written(); len(writes) != 0 {
			t.Errorf("%s: asked Docker too: %+v", verb, writes)
		}
	}
}

func TestPortainerCyclerLeavesAUnitsContainerAloneWhereNoUnitIsReached(t *testing.T) {
	for _, control := range []UnitControl{nil, &units{on: []string{"elsewhere"}}} {
		for _, apply := range []bool{true, false} {
			api, server := newPortainerAPI(t, map[string]string{"/api/endpoints": oneLocalEndpoint, containersPath: supervisedList})
			r := portainerRegistry(t, server.URL, runtime.Environment{})
			r.Units = control
			_, err := r.portainerCycler("stop")(t.Context(), PortainerContainerSpec{ConnectionRef: "portainer-example", Host: "hq-node", Name: "web"}, struct{}{}, apply)
			if err == nil || err.Error() != "web is run by web.service, and HQ cannot act on units on hq-node yet" {
				t.Fatalf("apply %v: got %v", apply, err)
			}
			if writes := api.written(); len(writes) != 0 {
				t.Errorf("Docker was asked: %+v", writes)
			}
		}
	}
}

func TestPortainerCyclerRefusesALabelNamingNoPlainUnit(t *testing.T) {
	list := strings.Replace(supervisedList, `"web.service"`, `"../../bin/sh"`, 1)
	api, server := newPortainerAPI(t, map[string]string{"/api/endpoints": oneLocalEndpoint, containersPath: list})
	r := portainerRegistry(t, server.URL, runtime.Environment{})
	control := &units{on: []string{"hq-node"}}
	r.Units = control
	if _, err := r.portainerCycler("stop")(t.Context(), PortainerContainerSpec{ConnectionRef: "portainer-example", Host: "hq-node", Name: "web"}, struct{}{}, true); err == nil {
		t.Fatal("a label naming no unit was acted on")
	}
	if len(control.acts) != 0 || len(api.written()) != 0 {
		t.Errorf("acted: %v %+v", control.acts, api.written())
	}
}

func TestAContainerRecordNamesTheUnitItsLabelNames(t *testing.T) {
	var listed []dockerContainer
	if err := json.Unmarshal([]byte(supervisedList), &listed); err != nil {
		t.Fatal(err)
	}
	if got := containerRecord(listed[0], "hq-node", "portainer-example", nil, "").Unit; got != "web.service" {
		t.Errorf("unit %q", got)
	}
	listed[0].Labels[unit.Label] = "web"
	if got := containerRecord(listed[0], "hq-node", "portainer-example", nil, "").Unit; got != "" {
		t.Errorf("a label naming no plain unit is recorded as %q", got)
	}
}

func TestPortainerProbe(t *testing.T) {
	_, server := newPortainerAPI(t, map[string]string{"/api/endpoints": `[{"Id":1,"Name":"local","URL":"unix:///x","Status":1},{"Id":2,"Name":"edge","URL":"tcp://192.0.2.20:9001","Status":1},{"Id":3,"Name":"down","URL":"tcp://192.0.2.30:9001","Status":2}]`})
	probe, err := portainerRegistry(t, server.URL, runtime.Environment{}).portainerProbe(t.Context(), "portainer-example")
	if err != nil || probe.Detail != "2 of 3 environments reachable." || !reflect.DeepEqual(probe.Reaches, []string{"edge", "hq-node"}) {
		t.Fatalf("%+v %v", probe, err)
	}
}

// A spec field of the wrong type refuses the action at the boundary.
func TestPortainerContainerSpecDecodesStrictly(t *testing.T) {
	r := New(runtime.Environment{}, supplied(), &fakeHTTP{})
	_, err := r.runAction(t.Context(), runtime.ResourceKindPortainerContainer, "restart", Object{"host": "hq-node", "name": 7}, nil, false)
	if err == nil {
		t.Fatal("a name given as a number is refused")
	}
}
