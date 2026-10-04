package providers

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/netip"
	"net/url"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Portainer holds one credential and reaches every Docker host registered with
// it, so a machine becomes available to HQ by being an environment there.
// Answers decode into the wire types in portainer_types.go.

const (
	portainerRunLabel  = "severino-hq.run"
	portainerNeeds     = "environment access"
	composeProject     = "com.docker.compose.project"
	composeWorkingDir  = "com.docker.compose.project.working_dir"
	composeConfigFiles = "com.docker.compose.project.config_files"
	composeService     = "com.docker.compose.service"
	imageSourceLabel   = "org.opencontainers.image.source"
	imageRevisionLabel = "org.opencontainers.image.revision"
	// portainerReachable is the Status Portainer gives an environment it can reach.
	portainerReachable = 1
	// dockerIDLength is the short container id Docker itself prints.
	dockerIDLength = 12
)

var (
	portainerEnvironmentTypes = map[int]string{1: "docker", 2: "agent", 3: "azure", 4: "edge agent", 5: "kubernetes", 6: "kubernetes agent", 7: "kubernetes edge agent"}
	portainerEnvironmentState = map[int]string{1: "up", 2: "down"}
	portainerStackState       = map[int]string{1: "active", 2: "inactive"}
)

func itoa(n int64) string { return strconv.FormatInt(n, 10) }

// PortainerEnvironment is one environment from GET /endpoints, as HQ files it.
type PortainerEnvironment struct {
	ID                int64  `json:"id"`
	Name              string `json:"name"`
	Address           string `json:"address"`
	Local             bool   `json:"local"`
	Reachable         bool   `json:"reachable"`
	Type              string `json:"type"`
	Status            string `json:"status"`
	AgentVersion      string `json:"agent_version"`
	DockerVersion     string `json:"docker_version"`
	ContainersRunning *int64 `json:"containers_running"`
	ContainersTotal   *int64 `json:"containers_total"`
	SnapshotAt        string `json:"snapshot_at"`
}

// PortainerContainerRecord is what Portainer knows about one container.
type PortainerContainerRecord struct {
	PortainerManaged bool   `json:"portainer_managed"`
	Name             string `json:"name"`
	ID               string `json:"id"`
	Stack            string `json:"stack"`
	WorkingDir       string `json:"working_dir"`
	Image            string `json:"image"`
	Source           string `json:"source"`
	Revision         string `json:"revision"`
	NetworkMode      string `json:"network_mode"`
	State            string `json:"state"`
	Status           string `json:"status"`
	Ports            []int  `json:"ports"`
	Port             *int   `json:"port"`
	Reachable        bool   `json:"reachable"`
	Host             string `json:"host"`
	HostAddress      string `json:"host_address"`
	ConnectionRef    string `json:"connection_ref"`
}

type PortainerStackSpec struct {
	ConnectionRef string            `json:"connection_ref"`
	Host          string            `json:"host"`
	Name          string            `json:"name"`
	Compose       string            `json:"compose"`
	Environment   []portainerEnvVar `json:"environment"`
	Port          *int              `json:"port"`
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
	State      string                     `json:"state"`
	Containers []PortainerContainerRecord `json:"containers"`
}

// portainerEnvVar is one value a stack's compose file reads, in the spec and in
// Portainer's Env list alike.
type portainerEnvVar struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// portainerStackPayload is the body of a stack create; an update adds PullImage.
type portainerStackPayload struct {
	Name             string            `json:"Name"`
	StackFileContent string            `json:"StackFileContent"`
	Env              []portainerEnvVar `json:"Env"`
	PullImage        *bool             `json:"PullImage,omitempty"`
}

func (r *Registry) admitPortainer() {
	act(r, runtime.ResourceKindPortainerStack, "reconcile", r.portainerReconcile)
	act(r, runtime.ResourceKindPortainerStack, "delete", r.portainerDelete)
	act(r, runtime.ResourceKindPortainerContainer, "restart", r.portainerCycler("restart"))
	act(r, runtime.ResourceKindPortainerContainer, "start", r.portainerCycler("start"))
	act(r, runtime.ResourceKindPortainerContainer, "stop", r.portainerCycler("stop"))
	r.reader(runtime.ResourceKindPortainerContainer, r.portainerInventory)
	r.reader(runtime.ResourceKindPortainerEnvironment, r.portainerEnvironmentReading)
	r.reader(runtime.ResourceKindPortainerNetwork, r.portainerEach("network list", r.portainerNetworks))
	r.reader(runtime.ResourceKindPortainerVolume, r.portainerEach("volume list", r.portainerVolumes))
	r.reader(runtime.ResourceKindPortainerImage, r.portainerEach("image list", r.portainerImages))
	r.reader(runtime.ResourceKindPortainerRuntime, r.portainerEach("container inspect", r.portainerRuntime))
	r.reader(runtime.ResourceKindPortainerComposeProject, r.portainerEach("stack list", r.portainerStacksReading))
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
		out = append(out, PublishedContainer{Host: record.Host, HostAddress: record.HostAddress, Ports: record.Ports})
	}
	return out, nil
}

// isThisRun matches the per-run nonce the launcher sets as a label and as
// HQ_CONTROLLER_RUN; a fixed label would let any container hide from the sweep.
func (r *Registry) isThisRun(labels map[string]string) bool {
	nonce := strings.TrimSpace(r.Env["HQ_CONTROLLER_RUN"])
	return nonce != "" && labels[portainerRunLabel] == nonce
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

// portainerGet GETs path and decodes it into T. A non-empty cache key shares
// the answer across the sweep.
func portainerGet[T any](ctx context.Context, r *Registry, ref, path, cacheKey, what string) (T, error) {
	load := func() (json.RawMessage, error) { return r.portainerCall(ctx, ref, path, "GET", nil) }
	var raw json.RawMessage
	var err error
	if cacheKey == "" {
		raw, err = load()
	} else {
		raw, err = r.cached(ctx, cacheKey, load)
	}
	if err != nil {
		var zero T
		return zero, err
	}
	return decodeAnswer[T](raw, "Portainer "+what)
}

// anAddress is a name resolved to the address it answers at, or the name when
// it does not resolve: an address is what every other source of a machine reports.
func (r *Registry) anAddress(host string) string {
	if host == "" || isIPAddress(host) {
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

// isIPAddress is a bare or bracketed address, with or without a port.
func isIPAddress(value string) bool {
	host := strings.TrimSpace(value)
	if strings.HasPrefix(host, "[") {
		host = strings.TrimPrefix(strings.SplitN(host, "]", 2)[0], "[")
	} else if strings.Count(host, ":") == 1 {
		host = host[:strings.LastIndex(host, ":")]
	}
	_, err := netip.ParseAddr(host)
	return host != "" && err == nil
}

// urlHostname is the lowercased host of a URL, or "" for none.
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
	listed, err := portainerGet[[]portainerEndpoint](ctx, r, ref, "/endpoints", "portainer-environments:"+ref, "environment list")
	if err != nil {
		return nil, err
	}
	base, err := r.portainerURL(ref)
	if err != nil {
		return nil, err
	}
	portainerAt := r.anAddress(urlHostname(base))
	found := make([]PortainerEnvironment, 0, len(listed))
	for _, endpoint := range listed {
		address := ""
		if strings.Contains(endpoint.URL, "://") {
			address = urlHostname(endpoint.URL)
		}
		found = append(found, portainerEnvironment(endpoint, address, portainerAt))
	}
	return found, nil
}

func portainerEnvironment(endpoint portainerEndpoint, address, portainerAt string) PortainerEnvironment {
	var snapshot portainerSnapshot
	if n := len(endpoint.Snapshots); n > 0 {
		snapshot = endpoint.Snapshots[n-1]
	}
	typeName, ok := portainerEnvironmentTypes[endpoint.Type]
	if !ok {
		typeName = strconv.Itoa(endpoint.Type)
	}
	located := address
	if located == "" {
		located = portainerAt
	}
	environment := PortainerEnvironment{
		ID:                endpoint.ID,
		Name:              endpoint.Name,
		Address:           located,
		Local:             address == "",
		Reachable:         endpoint.Status == portainerReachable,
		Type:              typeName,
		Status:            portainerEnvironmentState[endpoint.Status],
		DockerVersion:     snapshot.DockerVersion,
		ContainersRunning: snapshot.RunningContainerCount,
		ContainersTotal:   snapshot.ContainerCount,
		SnapshotAt:        unixStamp(snapshot.Time),
	}
	if endpoint.Agent != nil {
		environment.AgentVersion = endpoint.Agent.Version
	}
	return environment
}

// unixStamp is a Unix time in seconds as RFC 3339 in UTC, "" for none.
func unixStamp(seconds int64) string {
	if seconds <= 0 {
		return ""
	}
	return time.Unix(seconds, 0).UTC().Format(time.RFC3339)
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
		if (environment.Address != "" && environment.Address == host) || environment.Name == host {
			return environment, nil
		}
	}
	if local := r.controllerID(); local != "" && local == host {
		for _, environment := range environments {
			if environment.Local {
				return environment, nil
			}
		}
	}
	return PortainerEnvironment{}, &ProviderError{Message: fmt.Sprintf("no Portainer environment is %q", host)}
}

func (r *Registry) portainerStackList(ctx context.Context, ref string) ([]portainerStack, error) {
	return portainerGet[[]portainerStack](ctx, r, ref, "/stacks", "portainer-stacks:"+ref, "stack list")
}

// portainerStacksOn is the stacks Portainer created in one environment, optionally of one name.
func (r *Registry) portainerStacksOn(ctx context.Context, ref string, environment int64, name string) ([]portainerStack, error) {
	stacks, err := r.portainerStackList(ctx, ref)
	if err != nil {
		return nil, err
	}
	found := []portainerStack{}
	for _, stack := range stacks {
		if stack.EndpointID == environment && (name == "" || stack.Name == name) {
			found = append(found, stack)
		}
	}
	return found, nil
}

// portainerDocker is one Docker API GET through Portainer, shared across a sweep.
func portainerDocker[T any](ctx context.Context, r *Registry, ref string, environment int64, path, what string) (T, error) {
	id := itoa(environment)
	return portainerGet[T](ctx, r, ref, "/endpoints/"+id+"/docker"+path, "portainer-docker:"+ref+":"+id+":"+path, what)
}

func (r *Registry) portainerContainers(ctx context.Context, ref string, environment int64) ([]dockerContainer, error) {
	return portainerDocker[[]dockerContainer](ctx, r, ref, environment, "/containers/json?all=1", "container list")
}

// name is the container's first name without its leading slash.
func (c dockerContainer) name() string {
	if len(c.Names) == 0 {
		return ""
	}
	return strings.TrimLeft(c.Names[0], "/")
}

// named is whether any of the container's names is name.
func (c dockerContainer) named(name string) bool {
	return slices.ContainsFunc(c.Names, func(n string) bool { return strings.TrimLeft(n, "/") == name })
}

// published is every port a container answers on, the one that is
// unambiguous, and whether a loopback binding hides it from other machines.
func (c dockerContainer) published() ([]int, *int, bool) {
	seen := map[int]bool{}
	reachable := true
	for _, port := range c.Ports {
		if port.PublicPort == 0 {
			continue
		}
		seen[port.PublicPort] = true
		if port.IP == "127.0.0.1" || port.IP == "::1" {
			reachable = false
		}
	}
	listed := make([]int, 0, len(seen))
	for number := range seen {
		listed = append(listed, number)
	}
	sort.Ints(listed)
	var single *int
	if len(listed) == 1 {
		single = &listed[0]
	}
	return listed, single, reachable
}

func containerRecord(container dockerContainer, host, ref string, createdHere map[string]bool, hostAddress string) PortainerContainerRecord {
	ports, port, reachable := container.published()
	stack := container.Labels[composeProject]
	id := container.ID
	if len(id) > dockerIDLength {
		id = id[:dockerIDLength]
	}
	return PortainerContainerRecord{
		PortainerManaged: stack != "" && createdHere[stack],
		Name:             container.name(),
		ID:               id,
		Stack:            stack,
		WorkingDir:       container.Labels[composeWorkingDir],
		Image:            container.Image,
		Source:           container.Labels[imageSourceLabel],
		Revision:         container.Labels[imageRevisionLabel],
		NetworkMode:      container.HostConfig.NetworkMode,
		State:            container.State,
		Status:           container.Status,
		Ports:            ports,
		Port:             port,
		Reachable:        reachable,
		Host:             host,
		HostAddress:      hostAddress,
		ConnectionRef:    ref,
	}
}

func stackPayload(spec PortainerStackSpec) portainerStackPayload {
	env := spec.Environment
	if env == nil {
		env = []portainerEnvVar{}
	}
	return portainerStackPayload{Name: spec.Name, StackFileContent: spec.Compose, Env: env}
}

func (r *Registry) portainerReconcile(ctx context.Context, spec PortainerStackSpec, _ struct{}, apply bool) (Result, error) {
	ref := spec.ConnectionRef
	environment, err := r.portainerEnvironmentFor(ctx, spec.Host, ref)
	if err != nil {
		return Result{}, err
	}
	if !environment.Reachable {
		return Result{}, &ProviderError{Message: "Portainer cannot reach " + spec.Host}
	}
	existing, err := r.portainerStacksOn(ctx, ref, environment.ID, spec.Name)
	if err != nil {
		return Result{}, err
	}
	if len(existing) > 1 {
		return Result{}, &ProviderError{Message: fmt.Sprintf("Portainer holds more than one stack named %q", spec.Name)}
	}
	envID := itoa(environment.ID)
	changed := true
	if len(existing) == 1 {
		stackID := itoa(existing[0].ID)
		current, err := portainerGet[portainerStackFile](ctx, r, ref, "/stacks/"+stackID+"/file", "", "stack file")
		if err != nil {
			return Result{}, err
		}
		if current.StackFileContent == spec.Compose {
			changed = false
		} else if apply {
			payload := stackPayload(spec)
			pull := false
			payload.PullImage = &pull
			if _, err := r.portainerCall(ctx, ref, "/stacks/"+stackID+"?endpointId="+envID, "PUT", payload); err != nil {
				return Result{}, fmt.Errorf("update stack %s: %w", spec.Name, err)
			}
		}
	} else if apply {
		if _, err := r.portainerCall(ctx, ref, "/stacks/create/standalone/string?endpointId="+envID, "POST", stackPayload(spec)); err != nil {
			return Result{}, fmt.Errorf("create stack %s: %w", spec.Name, err)
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
		if container.Labels[composeProject] == spec.Name {
			containers = append(containers, containerRecord(container, spec.Host, ref, nil, ""))
		}
	}
	running, unreachable := 0, []string{}
	for _, item := range containers {
		if item.State == "running" {
			running++
		}
		if !item.Reachable {
			unreachable = append(unreachable, item.Name)
		}
	}
	status := PortainerStackStatus{Environment: environment.Name, Host: spec.Host, Containers: containers}
	if spec.Port != nil && *spec.Port > 0 {
		status.Origin = spec.Host + ":" + strconv.Itoa(*spec.Port)
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

func (r *Registry) portainerDelete(ctx context.Context, spec PortainerStackSpec, _ struct{}, apply bool) (Result, error) {
	ref := spec.ConnectionRef
	environment, err := r.portainerEnvironmentFor(ctx, spec.Host, ref)
	if err != nil {
		return Result{}, err
	}
	existing, err := r.portainerStacksOn(ctx, ref, environment.ID, spec.Name)
	if err != nil {
		return Result{}, err
	}
	if len(existing) == 0 {
		return result(false, struct{}{}, "Absent", "Stack is already gone.", "Stack was already absent."), nil
	}
	if apply {
		path := "/stacks/" + itoa(existing[0].ID) + "?endpointId=" + itoa(environment.ID)
		if _, err := r.portainerCall(ctx, ref, path, "DELETE", nil); err != nil {
			return Result{}, fmt.Errorf("delete stack %s: %w", spec.Name, err)
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
			stacks, err := r.portainerStacksOn(ctx, ref, environment.ID, "")
			if err != nil {
				return nil, err
			}
			createdHere := map[string]bool{}
			for _, stack := range stacks {
				if stack.Name != "" {
					createdHere[stack.Name] = true
				}
			}
			containers, err := r.portainerContainers(ctx, ref, environment.ID)
			if err != nil {
				return nil, err
			}
			for _, container := range containers {
				if !r.isThisRun(container.Labels) {
					records = append(records, containerRecord(container, host, ref, createdHere, environment.Address))
				}
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
		if container.named(spec.Name) {
			return container.ID, environment, nil
		}
	}
	return "", PortainerEnvironment{}, &ProviderError{Message: fmt.Sprintf("no container named %q on %s", spec.Name, spec.Host)}
}

// portainerCycler starts, stops or restarts one container and reports what it
// did. Docker answers 304 for a container already in the state asked for.
func (r *Registry) portainerCycler(verb string) func(context.Context, PortainerContainerSpec, struct{}, bool) (Result, error) {
	return func(ctx context.Context, spec PortainerContainerSpec, _ struct{}, apply bool) (Result, error) {
		containerID, environment, err := r.portainerContainerID(ctx, spec)
		if err != nil {
			return Result{}, err
		}
		if apply {
			path := "/endpoints/" + itoa(environment.ID) + "/docker/containers/" + containerID + "/" + verb
			if _, err := r.portainerCall(ctx, spec.ConnectionRef, path, "POST", struct{}{}); err != nil {
				return Result{}, fmt.Errorf("%s %s: %w", verb, spec.Name, err)
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
			if container.named(spec.Name) {
				observed = append(observed, containerRecord(container, spec.Host, spec.ConnectionRef, nil, ""))
			}
		}
		state := ""
		if len(observed) > 0 {
			state = observed[0].State
		}
		want := "running"
		if verb == "stop" {
			want = "exited"
		}
		kind := "Degraded"
		if state == want {
			kind = "Ready"
		}
		shown := state
		if shown == "" {
			shown = "in an unknown state"
		}
		said := spec.Name + " is " + shown + "."
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
		if environment.Reachable {
			reaches = append(reaches, machineName(environment, local))
		}
	}
	sort.Strings(reaches)
	return ProbeResult{Detail: fmt.Sprintf("%d of %d environments reachable.", len(reaches), len(environments)), Reaches: reaches}, nil
}
