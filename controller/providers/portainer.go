package providers

import (
	"context"
	"encoding/json"
	"net"
	"net/netip"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Portainer holds one credential and reaches every Docker host registered with
// it, so a machine becomes available to HQ by being an environment there.
//
// Portainer publishes Swagger 2.0 and most answers here are Docker Engine
// documents proxied through it, which that spec does not describe; the
// Python reads are lenient field by field, so answers decode into pyValue and
// are read through the accessors below.

const (
	portainerStackKind     = "portainer.stack"
	portainerContainerKind = "portainer.container"
	portainerRunLabel      = "severino-hq.run"
	portainerNeeds         = "environment access"
	composeProject         = "com.docker.compose.project"
	composeWorkingDir      = "com.docker.compose.project.working_dir"
	composeConfigFiles     = "com.docker.compose.project.config_files"
	composeService         = "com.docker.compose.service"
)

var (
	portainerEnvironmentTypes = map[int64]string{1: "docker", 2: "agent", 3: "azure", 4: "edge agent", 5: "kubernetes", 6: "kubernetes agent", 7: "kubernetes edge agent"}
	portainerEnvironmentState = map[int64]string{1: "up", 2: "down"}
	portainerStackState       = map[int64]string{1: "active", 2: "inactive"}
	pyEmptyObject             = pyValue{object: true}
	pyEmptyList               = pyValue{array: []pyValue{}}
)

// pyLookup is dict.get(value, "") on a table keyed by integers.
func pyLookup(table map[int64]string, value pyValue) (string, bool) {
	for key, name := range table {
		if value.eq(pyValue{literal: itoa(key)}) {
			return name, true
		}
	}
	return "", false
}

func itoa(n int64) string { return strconv.FormatInt(n, 10) }

// strOr is Python's str(x or "").
func strOr(value pyValue) string { return value.or(pyText("")).str() }

// items iterates a decoded list; any other value has none.
func items(value pyValue) []pyValue {
	if value.array == nil {
		return nil
	}
	return value.array
}

// PortainerEnvironment is one environment from GET /endpoints, as HQ files it.
type PortainerEnvironment struct {
	ID                pyValue `json:"id"`
	Name              string  `json:"name"`
	Address           string  `json:"address"`
	Local             bool    `json:"local"`
	Reachable         bool    `json:"reachable"`
	Type              string  `json:"type"`
	Status            string  `json:"status"`
	AgentVersion      string  `json:"agent_version"`
	DockerVersion     string  `json:"docker_version"`
	ContainersRunning pyValue `json:"containers_running"`
	ContainersTotal   pyValue `json:"containers_total"`
	SnapshotAt        string  `json:"snapshot_at"`
}

// PortainerContainerRecord is what Portainer knows about one container.
type PortainerContainerRecord struct {
	PortainerManaged bool    `json:"portainer_managed"`
	Name             string  `json:"name"`
	ID               string  `json:"id"`
	Stack            pyValue `json:"stack"`
	WorkingDir       pyValue `json:"working_dir"`
	Image            pyValue `json:"image"`
	Source           pyValue `json:"source"`
	Revision         pyValue `json:"revision"`
	NetworkMode      pyValue `json:"network_mode"`
	State            pyValue `json:"state"`
	Status           pyValue `json:"status"`
	Ports            []int64 `json:"ports"`
	Port             *int64  `json:"port"`
	Reachable        bool    `json:"reachable"`
	Host             string  `json:"host"`
	HostAddress      string  `json:"host_address"`
	ConnectionRef    string  `json:"connection_ref"`
}

type PortainerStackSpec struct {
	ConnectionRef string `json:"connection_ref"`
	Host          string `json:"host"`
	Name          string `json:"name"`
	Compose       string `json:"compose"`
	Environment   []struct {
		Name  pyValue `json:"name"`
		Value pyValue `json:"value"`
	} `json:"environment"`
	Port pyValue `json:"port"`
}

type PortainerStackStatus struct {
	Environment string                     `json:"environment"`
	Host        string                     `json:"host"`
	Containers  []PortainerContainerRecord `json:"containers"`
	Origin      string                     `json:"origin"`
	State       string                     `json:"state"`
}

type PortainerContainerSpec struct {
	ConnectionRef string `json:"connection_ref"`
	Host          string `json:"host"`
	Name          string `json:"name"`
}

type PortainerContainerStatus struct {
	Host       string                     `json:"host"`
	Container  string                     `json:"container"`
	State      pyValue                    `json:"state"`
	Containers []PortainerContainerRecord `json:"containers"`
}

type portainerEnvVar struct {
	Name  pyValue `json:"name"`
	Value pyValue `json:"value"`
}

// portainerStackPayload is the body of a stack create; an update adds PullImage.
type portainerStackPayload struct {
	Name             string            `json:"Name"`
	StackFileContent string            `json:"StackFileContent"`
	Env              []portainerEnvVar `json:"Env"`
	PullImage        *bool             `json:"PullImage,omitempty"`
}

func (r *Registry) admitPortainer() {
	r.action(portainerStackKind, "reconcile", r.portainerReconcile)
	r.action(portainerStackKind, "delete", r.portainerDelete)
	r.action(portainerContainerKind, "restart", r.portainerCycler("restart"))
	r.action(portainerContainerKind, "start", r.portainerCycler("start"))
	r.action(portainerContainerKind, "stop", r.portainerCycler("stop"))
	r.reader(portainerContainerKind, r.portainerInventory)
	r.reader("portainer.environment", r.portainerEnvironmentReading)
	r.reader("portainer.network", r.portainerEach("The network list", r.portainerNetworks))
	r.reader("portainer.volume", r.portainerEach("The volume list", r.portainerVolumes))
	r.reader("portainer.image", r.portainerEach("The image list", r.portainerImages))
	r.reader("portainer.runtime", r.portainerEach("Each container's inspect", r.portainerRuntime))
	r.reader("portainer.compose_project", r.portainerEach("The stack list", r.portainerStacksReading))
	r.probe("portainer", r.portainerProbe)
	if r.Portainer == nil {
		r.Portainer = registryPortainer{r}
	}
	if r.PublishedContainers == nil {
		r.PublishedContainers = r.portainerPublished
	}
}

// registryPortainer is the glance's view of this registry's Portainer access.
type registryPortainer struct{ r *Registry }

func (p registryPortainer) URL(ref string) (string, error) { return p.r.portainerURL(ref) }

func (p registryPortainer) Headers(ref string) (map[string]string, error) {
	return p.r.portainerHeaders(ref)
}

func (p registryPortainer) Environments(ctx context.Context, ref string) ([]PortainerEnvironment, error) {
	return p.r.portainerEnvironments(ctx, ref)
}

// portainerPublished is every container the sweep found and the ports it
// publishes: the one source host readings and the tailnet reach both read.
func (r *Registry) portainerPublished(ctx context.Context) ([]PublishedContainer, error) {
	records, err := r.portainerContainerRecords(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]PublishedContainer, 0, len(records))
	for _, record := range records {
		ports := make([]int, 0, len(record.Ports))
		for _, port := range record.Ports {
			ports = append(ports, int(port))
		}
		out = append(out, PublishedContainer{Host: record.Host, HostAddress: record.HostAddress, Ports: ports})
	}
	return out, nil
}

// isThisRun matches the per-run nonce the launcher sets as a label and as
// HQ_CONTROLLER_RUN; a fixed label would let any container hide from the sweep.
func (r *Registry) isThisRun(container pyValue) bool {
	nonce := strings.TrimSpace(r.Env["HQ_CONTROLLER_RUN"])
	return nonce != "" && container.get("Labels").or(pyEmptyObject).get(portainerRunLabel).eqText(nonce)
}

func (r *Registry) portainerURL(ref string) (string, error) {
	prefix, err := r.Env.Prefix("portainer", ref)
	if err != nil {
		return "", err
	}
	base, err := r.Env.Required(prefix, "URL")
	if err != nil {
		return "", err
	}
	base = strings.TrimRight(base, "/")
	if strings.HasSuffix(base, "/api") {
		return base, nil
	}
	return base + "/api", nil
}

func (r *Registry) portainerHeaders(ref string) (map[string]string, error) {
	prefix, err := r.Env.Prefix("portainer", ref)
	if err != nil {
		return nil, err
	}
	token, err := r.Env.Required(prefix, "API_TOKEN")
	if err != nil {
		return nil, err
	}
	return map[string]string{"X-API-Key": token}, nil
}

// portainerCall is one request to Portainer's API at path.
func (r *Registry) portainerCall(ctx context.Context, ref, path, method string, payload any) (json.RawMessage, error) {
	base, err := r.portainerURL(ref)
	if err != nil {
		return nil, err
	}
	headers, err := r.portainerHeaders(ref)
	if err != nil {
		return nil, err
	}
	return r.HTTP.Request(ctx, base+path, method, headers, payload)
}

func (r *Registry) portainerGet(ctx context.Context, ref, path, key string) (pyValue, error) {
	load := func() (json.RawMessage, error) { return r.portainerCall(ctx, ref, path, "GET", nil) }
	var raw json.RawMessage
	var err error
	if key == "" {
		raw, err = load()
	} else {
		raw, err = r.cached(ctx, key, load)
	}
	if err != nil {
		return pyValue{}, err
	}
	if len(raw) == 0 || string(raw) == "null" {
		return pyValue{literal: "null"}, nil
	}
	value, err := parsePy(raw)
	if err != nil {
		return pyValue{}, &ProviderError{Message: "Portainer returned an invalid answer."}
	}
	return value, nil
}

// anAddress is a name resolved to the address it answers at, or the name when
// it does not resolve: an address is what every other source of a machine reports.
func (r *Registry) anAddress(host string) string {
	if host == "" || pyParseIP(host) {
		return host
	}
	resolve := r.Resolve
	if resolve == nil {
		resolve = lookupIPv4
	}
	if address, err := resolve(host); err == nil && address != "" {
		return address
	}
	return host
}

func lookupIPv4(host string) (string, error) {
	addresses, err := net.LookupIP(host)
	if err != nil {
		return "", err
	}
	for _, address := range addresses {
		if v4 := address.To4(); v4 != nil {
			return v4.String(), nil
		}
	}
	return "", &net.DNSError{Err: "no IPv4 address", Name: host}
}

// pyParseIP is core.network.parse_ip: a bare or bracketed address, with or without a port.
func pyParseIP(value string) bool {
	host := strings.TrimSpace(value)
	if strings.HasPrefix(host, "[") {
		host = strings.TrimPrefix(strings.SplitN(host, "]", 2)[0], "[")
	} else if strings.Count(host, ":") == 1 {
		host = host[:strings.LastIndex(host, ":")]
	}
	_, err := netip.ParseAddr(host)
	return host != "" && err == nil
}

// urlHostname is urllib.parse.urlsplit(value).hostname, or "" for none.
func urlHostname(value string) string {
	parsed, err := url.Parse(value)
	if err != nil {
		return ""
	}
	return strings.ToLower(parsed.Hostname())
}

// portainerEnvironments is every environment, with the machine each one is.
// Portainer names its own local environment `local`; an agent carries the
// machine's address in its URL, and a unix socket is the machine Portainer runs on.
func (r *Registry) portainerEnvironments(ctx context.Context, ref string) ([]PortainerEnvironment, error) {
	listed, err := r.portainerGet(ctx, ref, "/endpoints", "portainer-environments:"+ref)
	if err != nil {
		return nil, err
	}
	base, err := r.portainerURL(ref)
	if err != nil {
		return nil, err
	}
	portainerAt := r.anAddress(urlHostname(base))
	found := []PortainerEnvironment{}
	for _, raw := range items(listed.or(pyEmptyList)) {
		at := raw.get("URL").orText("").str()
		address := ""
		if strings.Contains(at, "://") {
			address = urlHostname(at)
		}
		found = append(found, portainerEnvironment(raw, address, portainerAt))
	}
	return found, nil
}

func portainerEnvironment(raw pyValue, address, portainerAt string) PortainerEnvironment {
	snapshot := pyEmptyObject
	if snapshots := items(raw.get("Snapshots").or(pyEmptyList)); len(snapshots) > 0 && snapshots[len(snapshots)-1].object {
		snapshot = snapshots[len(snapshots)-1]
	}
	kind := raw.get("Type")
	typeName, ok := pyLookup(portainerEnvironmentTypes, kind)
	if !ok {
		typeName = strOr(kind)
	}
	status, _ := pyLookup(portainerEnvironmentState, raw.get("Status"))
	located := address
	if located == "" {
		located = portainerAt
	}
	return PortainerEnvironment{
		ID:                raw.get("Id"),
		Name:              strOr(raw.get("Name")),
		Address:           located,
		Local:             address == "",
		Reachable:         raw.get("Status").eq(pyValue{literal: "1"}),
		Type:              typeName,
		Status:            status,
		AgentVersion:      strOr(raw.get("Agent").or(pyEmptyObject).get("Version")),
		DockerVersion:     strOr(snapshot.get("DockerVersion")),
		ContainersRunning: snapshot.get("RunningContainerCount"),
		ContainersTotal:   snapshot.get("ContainerCount"),
		SnapshotAt:        pyStamp(snapshot.get("Time")),
	}
}

// pyStamp is a Unix time in seconds as ISO 8601 in UTC, "" for none or nonsense.
func pyStamp(seconds pyValue) string {
	value, ok := seconds.or(pyValue{literal: "0"}).toInt()
	if !ok || value <= 0 {
		return ""
	}
	return time.Unix(value, 0).UTC().Format("2006-01-02T15:04:05+00:00")
}

// machineName is the name HQ files an environment's machine under: a local
// socket is the machine the controller runs on.
func machineName(environment PortainerEnvironment, localHost string) string {
	if environment.Local && localHost != "" {
		return localHost
	}
	return environment.Name
}

// portainerEnvironmentFor is the environment that is a given machine: by
// address, by its own name, or for the local socket by the controller's machine.
func (r *Registry) portainerEnvironmentFor(ctx context.Context, host, ref string) (PortainerEnvironment, error) {
	environments, err := r.portainerEnvironments(ctx, ref)
	if err != nil {
		return PortainerEnvironment{}, err
	}
	for _, environment := range environments {
		if environment.Address != "" && environment.Address == host {
			return environment, nil
		}
		if environment.Name == host {
			return environment, nil
		}
	}
	local := r.controllerID()
	for _, environment := range environments {
		if environment.Local && local != "" && local == host {
			return environment, nil
		}
	}
	return PortainerEnvironment{}, &ProviderError{Message: "No Portainer environment is " + pyRepr(host) + "."}
}

func (r *Registry) portainerStackList(ctx context.Context, ref string) ([]pyValue, error) {
	listed, err := r.portainerGet(ctx, ref, "/stacks", "portainer-stacks:"+ref)
	if err != nil {
		return nil, err
	}
	return items(listed.or(pyEmptyList)), nil
}

func (r *Registry) portainerStacksOn(ctx context.Context, ref string, environment pyValue) ([]pyValue, error) {
	stacks, err := r.portainerStackList(ctx, ref)
	if err != nil {
		return nil, err
	}
	found := []pyValue{}
	for _, stack := range stacks {
		if stack.get("EndpointId").eq(environment) {
			found = append(found, stack)
		}
	}
	return found, nil
}

// portainerDocker is one Docker API call through Portainer, shared across a sweep.
func (r *Registry) portainerDocker(ctx context.Context, ref string, environment pyValue, path string) (pyValue, error) {
	id := environment.str()
	return r.portainerGet(ctx, ref, "/endpoints/"+id+"/docker"+path, "portainer-docker:"+ref+":"+id+":"+path)
}

func (r *Registry) portainerContainers(ctx context.Context, ref string, environment pyValue) ([]pyValue, error) {
	listed, err := r.portainerDocker(ctx, ref, environment, "/containers/json?all=1")
	if err != nil {
		return nil, err
	}
	return items(listed.or(pyEmptyList)), nil
}

// containerName is the container's first name without its leading slash.
func containerName(container pyValue) string {
	names := items(container.get("Names").or(pyValue{array: []pyValue{pyText("/")}}))
	if len(names) == 0 {
		return ""
	}
	return strings.TrimLeft(names[0].str(), "/")
}

func containerNames(container pyValue) []string {
	names := []string{}
	for _, name := range items(container.get("Names").or(pyEmptyList)) {
		names = append(names, strings.TrimLeft(name.str(), "/"))
	}
	return names
}

// published is every port a container answers on, the one that is
// unambiguous, and whether a loopback binding hides it from other machines.
func published(container pyValue) ([]int64, *int64, bool, error) {
	seen := map[int64]bool{}
	reachable := true
	for _, port := range items(container.get("Ports").or(pyEmptyList)) {
		public := port.get("PublicPort")
		if !public.truthy() {
			continue
		}
		number, ok := public.toInt()
		if !ok {
			return nil, nil, false, &ProviderError{Message: "Portainer returned an invalid port."}
		}
		seen[number] = true
		if ip := port.get("IP").orText("").str(); ip == "127.0.0.1" || ip == "::1" {
			reachable = false
		}
	}
	listed := []int64{}
	for number := range seen {
		listed = append(listed, number)
	}
	sort.Slice(listed, func(a, b int) bool { return listed[a] < listed[b] })
	var single *int64
	if len(listed) == 1 {
		single = &listed[0]
	}
	return listed, single, reachable, nil
}

func containerRecord(container pyValue, host, ref string, createdHere map[string]bool, hostAddress string) (PortainerContainerRecord, error) {
	labels := container.get("Labels").or(pyEmptyObject)
	ports, port, reachable, err := published(container)
	if err != nil {
		return PortainerContainerRecord{}, err
	}
	stack := labels.get(composeProject).orText("")
	id := []rune(container.get("Id").orText("").str())
	if len(id) > 12 {
		id = id[:12]
	}
	return PortainerContainerRecord{
		PortainerManaged: stack.truthy() && stack.text != nil && createdHere[*stack.text],
		Name:             containerName(container),
		ID:               string(id),
		Stack:            stack,
		WorkingDir:       labels.get(composeWorkingDir).orText(""),
		Image:            container.get("Image").orText(""),
		Source:           labels.get("org.opencontainers.image.source").orText(""),
		Revision:         labels.get("org.opencontainers.image.revision").orText(""),
		NetworkMode:      container.get("HostConfig").or(pyEmptyObject).get("NetworkMode").orText(""),
		State:            container.get("State").orText(""),
		Status:           container.get("Status").orText(""),
		Ports:            ports,
		Port:             port,
		Reachable:        reachable,
		Host:             host,
		HostAddress:      hostAddress,
		ConnectionRef:    ref,
	}, nil
}

func stackPayload(spec PortainerStackSpec) portainerStackPayload {
	env := []portainerEnvVar{}
	for _, item := range spec.Environment {
		env = append(env, portainerEnvVar{Name: item.Name.orText(""), Value: item.Value.orText("")})
	}
	return portainerStackPayload{Name: spec.Name, StackFileContent: spec.Compose, Env: env}
}

func (r *Registry) namedStacks(ctx context.Context, ref string, environment pyValue, name string) ([]pyValue, error) {
	stacks, err := r.portainerStacksOn(ctx, ref, environment)
	if err != nil {
		return nil, err
	}
	found := []pyValue{}
	for _, stack := range stacks {
		if stack.get("Name").eqText(name) {
			found = append(found, stack)
		}
	}
	return found, nil
}

func (r *Registry) portainerReconcile(ctx context.Context, rawSpec, _ Object, apply bool) (Result, error) {
	spec, err := decodePayload[PortainerStackSpec](rawSpec)
	if err != nil {
		return Result{}, err
	}
	ref := spec.ConnectionRef
	environment, err := r.portainerEnvironmentFor(ctx, spec.Host, ref)
	if err != nil {
		return Result{}, err
	}
	if !environment.Reachable {
		return Result{}, &ProviderError{Message: "Portainer cannot currently reach " + spec.Host + "."}
	}
	existing, err := r.namedStacks(ctx, ref, environment.ID, spec.Name)
	if err != nil {
		return Result{}, err
	}
	if len(existing) > 1 {
		return Result{}, &ProviderError{Message: "Portainer holds more than one stack of that name."}
	}
	envID := environment.ID.str()
	changed := true
	if len(existing) == 1 {
		stackID := existing[0].get("Id").str()
		current, err := r.portainerGet(ctx, ref, "/stacks/"+stackID+"/file", "")
		if err != nil {
			return Result{}, err
		}
		if current.or(pyEmptyObject).get("StackFileContent").eqText(spec.Compose) {
			changed = false
		} else if apply {
			payload := stackPayload(spec)
			pull := false
			payload.PullImage = &pull
			if _, err := r.portainerCall(ctx, ref, "/stacks/"+stackID+"?endpointId="+envID, "PUT", payload); err != nil {
				return Result{}, err
			}
		}
	} else if apply {
		if _, err := r.portainerCall(ctx, ref, "/stacks/create/standalone/string?endpointId="+envID, "POST", stackPayload(spec)); err != nil {
			return Result{}, err
		}
	}

	// What is running is the only thing worth reporting: a stack Portainer
	// accepted and Docker then failed to start is not Ready.
	listed, err := r.portainerContainers(ctx, ref, environment.ID)
	if err != nil {
		return Result{}, err
	}
	containers := []PortainerContainerRecord{}
	for _, container := range listed {
		if !container.get("Labels").or(pyEmptyObject).get(composeProject).eqText(spec.Name) {
			continue
		}
		record, err := containerRecord(container, spec.Host, ref, nil, "")
		if err != nil {
			return Result{}, err
		}
		containers = append(containers, record)
	}
	running, unreachable := 0, []string{}
	for _, item := range containers {
		if item.State.eqText("running") {
			running++
		}
		if !item.Reachable {
			unreachable = append(unreachable, item.Name)
		}
	}
	status := PortainerStackStatus{Environment: environment.Name, Host: spec.Host, Containers: containers}
	if spec.Port.truthy() {
		status.Origin = spec.Host + ":" + spec.Port.str()
	}
	if running > 0 && running == len(containers) {
		status.State = "running"
	}
	message := "Stack unchanged."
	if changed {
		message = "Stack updated."
	}
	switch {
	case apply && len(containers) == 0:
		return Result{Changed: changed, Status: status, Conditions: []Condition{condition("Degraded", "NotRunning", "The stack exists in Portainer but no container from it is running.")}, Message: "Stack is declared but nothing is running."}, nil
	case len(unreachable) > 0:
		sort.Strings(unreachable)
		detail := strings.Join(unreachable, ", ") + " publishes a port on the loopback address, so nothing outside that machine can reach it, including a proxy running in a container on the same host."
		return Result{Changed: changed, Status: status, Conditions: []Condition{condition("Degraded", "BoundToLoopback", detail)}, Message: "Stack is running but is not reachable."}, nil
	}
	return result(changed, status, "Reconciled", "Stack is running.", message), nil
}

func (r *Registry) portainerDelete(ctx context.Context, rawSpec, _ Object, apply bool) (Result, error) {
	spec, err := decodePayload[PortainerStackSpec](rawSpec)
	if err != nil {
		return Result{}, err
	}
	ref := spec.ConnectionRef
	environment, err := r.portainerEnvironmentFor(ctx, spec.Host, ref)
	if err != nil {
		return Result{}, err
	}
	existing, err := r.namedStacks(ctx, ref, environment.ID, spec.Name)
	if err != nil {
		return Result{}, err
	}
	if len(existing) == 0 {
		return result(false, struct{}{}, "Absent", "Stack is already gone.", "Stack was already absent."), nil
	}
	if apply {
		path := "/stacks/" + existing[0].get("Id").str() + "?endpointId=" + environment.ID.str()
		if _, err := r.portainerCall(ctx, ref, path, "DELETE", nil); err != nil {
			return Result{}, err
		}
	}
	return result(true, struct{}{}, "Deleted", "Stack removed.", "Stack removed."), nil
}

// portainerContainerRecords is every container Portainer can see, on every
// machine it reaches. Containers, not stacks: Portainer lists only the stacks it
// created, and Docker will cycle any container.
func (r *Registry) portainerContainerRecords(ctx context.Context) ([]PortainerContainerRecord, error) {
	local := r.controllerID()
	records := []PortainerContainerRecord{}
	for _, ref := range r.Env.Refs("portainer") {
		environments, err := r.portainerEnvironments(ctx, ref)
		if err != nil {
			return nil, err
		}
		for _, environment := range environments {
			if !environment.Reachable {
				continue
			}
			host := machineName(environment, local)
			stacks, err := r.portainerStacksOn(ctx, ref, environment.ID)
			if err != nil {
				return nil, err
			}
			createdHere := map[string]bool{}
			for _, stack := range stacks {
				if stack.get("Name").truthy() {
					createdHere[stack.get("Name").str()] = true
				}
			}
			containers, err := r.portainerContainers(ctx, ref, environment.ID)
			if err != nil {
				return nil, err
			}
			for _, container := range containers {
				if r.isThisRun(container) {
					continue
				}
				record, err := containerRecord(container, host, ref, createdHere, environment.Address)
				if err != nil {
					return nil, err
				}
				records = append(records, record)
			}
		}
	}
	return records, nil
}

func (r *Registry) portainerInventory(ctx context.Context) ([]any, error) {
	records, err := r.portainerContainerRecords(ctx)
	if err != nil {
		return nil, err
	}
	found := make([]any, len(records))
	for i, record := range records {
		found[i] = record
	}
	return found, nil
}

// portainerContainerID is the Docker id of a declared container, looked up by
// name on each pass: a recreated container gets a new id.
func (r *Registry) portainerContainerID(ctx context.Context, spec PortainerContainerSpec) (string, PortainerEnvironment, error) {
	environment, err := r.portainerEnvironmentFor(ctx, spec.Host, spec.ConnectionRef)
	if err != nil {
		return "", PortainerEnvironment{}, err
	}
	containers, err := r.portainerContainers(ctx, spec.ConnectionRef, environment.ID)
	if err != nil {
		return "", PortainerEnvironment{}, err
	}
	for _, container := range containers {
		if indexOf(containerNames(container), spec.Name) >= 0 {
			return container.get("Id").orText("").str(), environment, nil
		}
	}
	return "", PortainerEnvironment{}, &ProviderError{Message: "No container named " + pyRepr(spec.Name) + " on " + spec.Host + "."}
}

// portainerCycler starts, stops or restarts one container and reports what it
// did. Docker answers 304 for a container already in the state asked for.
func (r *Registry) portainerCycler(verb string) Action {
	return func(ctx context.Context, rawSpec, _ Object, apply bool) (Result, error) {
		spec, err := decodePayload[PortainerContainerSpec](rawSpec)
		if err != nil {
			return Result{}, err
		}
		containerID, environment, err := r.portainerContainerID(ctx, spec)
		if err != nil {
			return Result{}, err
		}
		if apply {
			path := "/endpoints/" + environment.ID.str() + "/docker/containers/" + containerID + "/" + verb
			if _, err := r.portainerCall(ctx, spec.ConnectionRef, path, "POST", struct{}{}); err != nil {
				return Result{}, err
			}
		}
		// Read back rather than trusting the call: a restart that exits two
		// seconds later reports success at the API.
		containers, err := r.portainerContainers(ctx, spec.ConnectionRef, environment.ID)
		if err != nil {
			return Result{}, err
		}
		observed := []PortainerContainerRecord{}
		for _, container := range containers {
			if indexOf(containerNames(container), spec.Name) < 0 {
				continue
			}
			record, err := containerRecord(container, spec.Host, spec.ConnectionRef, nil, "")
			if err != nil {
				return Result{}, err
			}
			observed = append(observed, record)
		}
		state := pyText("")
		if len(observed) > 0 {
			state = observed[0].State
		}
		want := "running"
		if verb == "stop" {
			want = "exited"
		}
		kind := "Degraded"
		if state.eqText(want) {
			kind = "Ready"
		}
		said := spec.Name + " is " + state.or(pyText("in an unknown state")).str() + "."
		status := PortainerContainerStatus{Host: spec.Host, Container: spec.Name, State: state, Containers: observed}
		return Result{Changed: apply, Status: status, Conditions: []Condition{condition(kind, strings.ToUpper(verb[:1])+verb[1:]+"ed", said)}, Message: said}, nil
	}
}

func (r *Registry) portainerProbe(ctx context.Context, ref string) (ProbeResult, error) {
	environments, err := r.portainerEnvironments(ctx, ref)
	if err != nil {
		return ProbeResult{}, err
	}
	local := r.controllerID()
	reaches := []string{}
	for _, environment := range environments {
		if !environment.Reachable {
			continue
		}
		if environment.Local && local != "" {
			reaches = append(reaches, local)
		} else {
			reaches = append(reaches, environment.Name)
		}
	}
	sort.Strings(reaches)
	return ProbeResult{Detail: itoa(int64(len(reaches))) + " of " + itoa(int64(len(environments))) + " environments reachable.", Reaches: reaches}, nil
}
