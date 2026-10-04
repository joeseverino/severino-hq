package providers

import (
	"context"
	"encoding/json"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// portainerAPI is Portainer behind a real HTTP server: GET answers by path and
// query, writes recorded, any route answerable with a status instead.
type portainerAPI struct {
	t      *testing.T
	mu     sync.Mutex
	routes map[string]string // path?query -> JSON body
	status map[string]int    // path?query -> non-2xx status
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
	if !ok {
		a.t.Errorf("unexpected GET %s", key)
		w.WriteHeader(http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	io.WriteString(w, answer)
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
	env := runtime.Environment{
		"PORTAINER_CONNECTION_REF": "portainer-example",
		"PORTAINER_URL":            base,
		"PORTAINER_API_TOKEN":      "synthetic",
		"HQ_CONTROLLER_ID":         "hq-node",
	}
	maps.Copy(env, extra)
	r := New(env, client)
	r.Resolve = func(string) (string, error) { return "192.0.2.100", nil }
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
	r := portainerRegistry(t, server.URL, nil)
	found, err := r.portainerEnvironments(t.Context(), "portainer-example")
	if err != nil || len(found) != 3 {
		t.Fatalf("%+v %v", found, err)
	}
	three, four := int64(3), int64(4)
	want := []PortainerEnvironment{
		{ID: 1, Name: "local", Address: urlHostname(server.URL), Local: true, Reachable: true, Type: "docker", Status: "up", AgentVersion: "2.19.4",
			DockerVersion: "27.1", ContainersRunning: &three, ContainersTotal: &four, SnapshotAt: "2026-01-02T12:00:00Z"},
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
			err := c.read(portainerRegistry(t, server.URL, nil))
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
		{500, runtime.RefusalUnclassified, "environment list: provider answered 500"},
	}
	for _, c := range cases {
		api, server := newPortainerAPI(t, map[string]string{})
		api.status["/api/endpoints"] = c.code
		r := portainerRegistry(t, server.URL, nil)
		_, err := r.portainerEnvironmentReading(t.Context())
		if _, refusal, _ := runtime.Classify(err); refusal != c.refusal || !strings.HasPrefix(err.Error(), c.says) {
			t.Errorf("%d: %q %v", c.code, refusal, err)
		}
	}
}

// The sweep skips the controller's own run by the nonce it was handed, so a
// container cannot hide by wearing the label with another value.
func TestPortainerSweepSkipsOnlyThisRun(t *testing.T) {
	_, server := newPortainerAPI(t, map[string]string{
		"/api/endpoints": oneLocalEndpoint,
		"/api/stacks":    `[]`,
		containersPath: `[
			{"Id":"aaaaaaaaaaaaaaaa","Names":["/hq-controller"],"Labels":{"severino-hq.run":"nonce-1"},"State":"running"},
			{"Id":"bbbbbbbbbbbbbbbb","Names":["/impostor"],"Labels":{"severino-hq.run":"nonce-2"},"State":"running"},
			{"Id":"cccccccccccccccc","Names":["/web"],"State":"running"}]`,
	})
	r := portainerRegistry(t, server.URL, runtime.Environment{"HQ_CONTROLLER_RUN": "nonce-1"})
	records, err := r.portainerContainerRecords(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	names := []string{}
	for _, record := range records {
		names = append(names, record.Name)
	}
	if !reflect.DeepEqual(names, []string{"impostor", "web"}) {
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
	r := portainerRegistry(t, server.URL, nil)
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
	r := portainerRegistry(t, server.URL, nil)
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
	r = portainerRegistry(t, server.URL, nil)
	if _, err := r.readers[string(runtime.ResourceKindPortainerNetwork)](t.Context()); err == nil || err.Error() != "network list needs environment access: provider answered 403" {
		t.Fatalf("every machine refusing refuses the reading: %v", err)
	}
}

func stackRoutes(stackFile string, stacks string, containers string) map[string]string {
	return map[string]string{
		"/api/endpoints":      oneLocalEndpoint,
		"/api/stacks":         stacks,
		"/api/stacks/10/file": stackFile,
		containersPath:        containers,
	}
}

func TestPortainerStackReconcileDecisions(t *testing.T) {
	running := `[{"Id":"a1","Names":["/web-app-1"],"Labels":{"com.docker.compose.project":"web"},"State":"running","Ports":[{"IP":"0.0.0.0","PublicPort":8080}]}]`
	loopback := `[{"Id":"a1","Names":["/web-app-1"],"Labels":{"com.docker.compose.project":"web"},"State":"running","Ports":[{"IP":"127.0.0.1","PublicPort":8080}]}]`
	existing := `[{"Id":10,"Name":"web","EndpointId":1}]`
	port := 8080
	spec := PortainerStackSpec{ConnectionRef: "portainer-example", Host: "hq-node", Name: "web", Compose: "services: {}",
		Environment: []portainerEnvVar{{Name: "A", Value: "1"}}, Port: &port}
	cases := []struct {
		name       string
		stacks     string
		file       string
		containers string
		apply      bool
		changed    bool
		reason     string
		writes     []string
	}{
		{"unchanged", existing, `{"StackFileContent":"services: {}"}`, running, true, false, "Reconciled", nil},
		{"changed, planned", existing, `{"StackFileContent":"old"}`, running, false, true, "Reconciled", nil},
		{"changed, applied", existing, `{"StackFileContent":"old"}`, running, true, true, "Reconciled", []string{"PUT /api/stacks/10?endpointId=1"}},
		{"absent, applied", `[]`, `null`, running, true, true, "Reconciled", []string{"POST /api/stacks/create/standalone/string?endpointId=1"}},
		{"applied, nothing runs", existing, `{"StackFileContent":"services: {}"}`, `[]`, true, false, "NotRunning", nil},
		{"bound to loopback", existing, `{"StackFileContent":"services: {}"}`, loopback, true, false, "BoundToLoopback", nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			api, server := newPortainerAPI(t, stackRoutes(c.file, c.stacks, c.containers))
			r := portainerRegistry(t, server.URL, nil)
			res, err := r.portainerReconcile(t.Context(), spec, struct{}{}, c.apply)
			if err != nil {
				t.Fatal(err)
			}
			if res.Changed != c.changed || res.Conditions[0].Reason != c.reason {
				t.Fatalf("%+v", res)
			}
			writes := []string{}
			for _, w := range api.written() {
				writes = append(writes, w.method+" "+w.path)
				if w.body["Name"] != "web" || w.body["StackFileContent"] != "services: {}" || !reflect.DeepEqual(w.body["Env"], []any{map[string]any{"name": "A", "value": "1"}}) {
					t.Errorf("payload %v", w.body)
				}
				if w.method == "PUT" && w.body["PullImage"] != false {
					t.Errorf("an update does not pull: %v", w.body)
				}
			}
			if len(writes) != len(c.writes) || (len(writes) > 0 && writes[0] != c.writes[0]) {
				t.Fatalf("writes %v, want %v", writes, c.writes)
			}
			if status := res.Status.(PortainerStackStatus); status.Origin != "hq-node:8080" {
				t.Errorf("origin %q", status.Origin)
			}
		})
	}
}

func TestPortainerStackReconcileRefusals(t *testing.T) {
	spec := PortainerStackSpec{ConnectionRef: "portainer-example", Host: "hq-node", Name: "web", Compose: "x"}
	cases := []struct {
		name, endpoints, stacks, want string
	}{
		{"no such machine", oneLocalEndpoint, `[]`, `no Portainer environment is "elsewhere"`},
		{"machine down", `[{"Id":1,"Name":"local","URL":"unix:///var/run/docker.sock","Status":2}]`, `[]`, "Portainer cannot reach hq-node"},
		{"two stacks of the name", oneLocalEndpoint, `[{"Id":10,"Name":"web","EndpointId":1},{"Id":11,"Name":"web","EndpointId":1}]`, `Portainer holds more than one stack named "web"`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			api, server := newPortainerAPI(t, map[string]string{"/api/endpoints": c.endpoints, "/api/stacks": c.stacks})
			r := portainerRegistry(t, server.URL, nil)
			s := spec
			if c.name == "no such machine" {
				s.Host = "elsewhere"
			}
			_, err := r.portainerReconcile(t.Context(), s, struct{}{}, true)
			if err == nil || err.Error() != c.want {
				t.Fatalf("got %v", err)
			}
			if len(api.written()) != 0 {
				t.Fatal("a refusal writes nothing")
			}
		})
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
		r := portainerRegistry(t, server.URL, nil)
		res, err := r.portainerCycler(c.verb)(t.Context(), PortainerContainerSpec{ConnectionRef: "portainer-example", Host: "hq-node", Name: "web"}, struct{}{}, true)
		if err != nil || string(res.Conditions[0].Type) != c.kind || res.Message != "web is "+c.state+"." {
			t.Errorf("%s: %+v %v", c.verb, res, err)
		}
		if writes := api.written(); len(writes) != 1 || writes[0].path != "/api/endpoints/1/docker/containers/def/"+c.verb {
			t.Errorf("%s: %+v", c.verb, writes)
		}
	}
	_, server := newPortainerAPI(t, map[string]string{"/api/endpoints": oneLocalEndpoint, containersPath: `[]`})
	r := portainerRegistry(t, server.URL, nil)
	if _, err := r.portainerCycler("stop")(t.Context(), PortainerContainerSpec{ConnectionRef: "portainer-example", Host: "hq-node", Name: "web"}, struct{}{}, true); err == nil || err.Error() != `no container named "web" on hq-node` {
		t.Fatalf("got %v", err)
	}
}

func TestPortainerProbe(t *testing.T) {
	_, server := newPortainerAPI(t, map[string]string{"/api/endpoints": `[{"Id":1,"Name":"local","URL":"unix:///x","Status":1},{"Id":2,"Name":"edge","URL":"tcp://192.0.2.20:9001","Status":1},{"Id":3,"Name":"down","URL":"tcp://192.0.2.30:9001","Status":2}]`})
	probe, err := portainerRegistry(t, server.URL, nil).portainerProbe(t.Context(), "portainer-example")
	if err != nil || probe.Detail != "2 of 3 environments reachable." || !reflect.DeepEqual(probe.Reaches, []string{"edge", "hq-node"}) {
		t.Fatalf("%+v %v", probe, err)
	}
}

// A stack spec field of the wrong type refuses the action at the boundary.
func TestPortainerStackSpecDecodesStrictly(t *testing.T) {
	r := New(runtime.Environment{}, &fakeHTTP{})
	_, err := r.runAction(runtime.ResourceKindPortainerStack, "reconcile", t.Context(), Object{"name": "web", "port": "8080"}, nil, false)
	if err == nil {
		t.Fatal("a port given as text is refused")
	}
}
