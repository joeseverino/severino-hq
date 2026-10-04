package providers

import (
	"encoding/json"
	"fmt"
)

// Wire types for the Portainer and Docker Engine answers HQ reads. Portainer's
// own endpoints follow its API; everything under /endpoints/{id}/docker is the
// Docker Engine API proxied through it. Only the fields HQ reads are declared,
// each with the JSON type the API sends, so a field of another type fails the
// decode instead of being coerced.

// portainerEndpoint is one environment from GET /endpoints.
type portainerEndpoint struct {
	ID        int64               `json:"Id"`
	Name      string              `json:"Name"`
	URL       string              `json:"URL"`
	Type      int                 `json:"Type"`
	Status    int                 `json:"Status"`
	Agent     *portainerAgent     `json:"Agent"`
	Snapshots []portainerSnapshot `json:"Snapshots"`
}

type portainerAgent struct {
	Version string `json:"Version"`
}

// portainerSnapshot is Portainer's periodic picture of a Docker environment.
type portainerSnapshot struct {
	DockerVersion         string `json:"DockerVersion"`
	Time                  int64  `json:"Time"`
	RunningContainerCount *int64 `json:"RunningContainerCount"`
	ContainerCount        *int64 `json:"ContainerCount"`
}

// portainerStack is one stack from GET /stacks.
type portainerStack struct {
	ID          int64  `json:"Id"`
	Name        string `json:"Name"`
	EndpointID  int64  `json:"EndpointId"`
	Status      int    `json:"Status"`
	EntryPoint  string `json:"EntryPoint"`
	ProjectPath string `json:"ProjectPath"`
}

// portainerStackFile is GET /stacks/{id}/file.
type portainerStackFile struct {
	StackFileContent string `json:"StackFileContent"`
}

// dockerContainer is one item of GET /containers/json.
type dockerContainer struct {
	ID         string            `json:"Id"`
	Names      []string          `json:"Names"`
	Image      string            `json:"Image"`
	ImageID    string            `json:"ImageID"`
	Labels     map[string]string `json:"Labels"`
	State      string            `json:"State"`
	Status     string            `json:"Status"`
	Ports      []dockerPort      `json:"Ports"`
	HostConfig struct {
		NetworkMode string `json:"NetworkMode"`
	} `json:"HostConfig"`
	NetworkSettings struct {
		Networks map[string]struct{} `json:"Networks"`
	} `json:"NetworkSettings"`
	Mounts []dockerMount `json:"Mounts"`
}

type dockerPort struct {
	IP          string `json:"IP"`
	PrivatePort int    `json:"PrivatePort"`
	PublicPort  int    `json:"PublicPort"`
	Type        string `json:"Type"`
}

type dockerMount struct {
	Type        string `json:"Type"`
	Name        string `json:"Name"`
	Source      string `json:"Source"`
	Destination string `json:"Destination"`
	RW          *bool  `json:"RW"`
}

// readOnly is a mount Docker says is not writable; one that does not say is not read-only.
func (m dockerMount) readOnly() bool { return m.RW != nil && !*m.RW }

// key is what names a mount: a volume by its name, a bind by its source.
func (m dockerMount) key() string {
	if m.Type == "volume" {
		return m.Name
	}
	return m.Source
}

type dockerNetwork struct {
	ID       string `json:"Id"`
	Name     string `json:"Name"`
	Driver   string `json:"Driver"`
	Scope    string `json:"Scope"`
	Internal bool   `json:"Internal"`
	IPAM     struct {
		Config []struct {
			Subnet string `json:"Subnet"`
		} `json:"Config"`
	} `json:"IPAM"`
}

// dockerVolumes is GET /volumes.
type dockerVolumes struct {
	Volumes []struct {
		Name       string            `json:"Name"`
		Driver     string            `json:"Driver"`
		Mountpoint string            `json:"Mountpoint"`
		CreatedAt  string            `json:"CreatedAt"`
		Labels     map[string]string `json:"Labels"`
	} `json:"Volumes"`
}

type dockerImage struct {
	ID          string   `json:"Id"`
	RepoTags    []string `json:"RepoTags"`
	RepoDigests []string `json:"RepoDigests"`
	Created     int64    `json:"Created"`
	Size        int64    `json:"Size"`
}

// dockerInspect is GET /containers/{id}/json, without the environment: HQ never
// copies a container's variables out.
type dockerInspect struct {
	Image  string `json:"Image"`
	Config struct {
		User         string              `json:"User"`
		Labels       map[string]string   `json:"Labels"`
		ExposedPorts map[string]struct{} `json:"ExposedPorts"`
		Healthcheck  *struct {
			Test []string `json:"Test"`
		} `json:"Healthcheck"`
	} `json:"Config"`
	HostConfig struct {
		Privileged     bool     `json:"Privileged"`
		ReadonlyRootfs bool     `json:"ReadonlyRootfs"`
		NetworkMode    string   `json:"NetworkMode"`
		PidMode        string   `json:"PidMode"`
		IpcMode        string   `json:"IpcMode"`
		CapAdd         []string `json:"CapAdd"`
		CapDrop        []string `json:"CapDrop"`
		SecurityOpt    []string `json:"SecurityOpt"`
		Devices        []struct {
			PathOnHost string `json:"PathOnHost"`
		} `json:"Devices"`
		PortBindings map[string][]struct {
			HostIP   string `json:"HostIp"`
			HostPort string `json:"HostPort"`
		} `json:"PortBindings"`
		Memory        int64  `json:"Memory"`
		NanoCpus      int64  `json:"NanoCpus"`
		PidsLimit     *int64 `json:"PidsLimit"`
		RestartPolicy struct {
			Name string `json:"Name"`
		} `json:"RestartPolicy"`
	} `json:"HostConfig"`
	State struct {
		Health *struct {
			Status string `json:"Status"`
		} `json:"Health"`
		StartedAt string `json:"StartedAt"`
	} `json:"State"`
	Mounts       []dockerMount `json:"Mounts"`
	RestartCount int64         `json:"RestartCount"`
}

// dockerInfo is GET /info.
type dockerInfo struct {
	NCPU     int64 `json:"NCPU"`
	MemTotal int64 `json:"MemTotal"`
}

// dockerDiskUsage is GET /system/df. Docker answers -1 for a size it did not compute.
type dockerDiskUsage struct {
	LayersSize int64 `json:"LayersSize"`
	Volumes    []struct {
		UsageData *struct {
			Size int64 `json:"Size"`
		} `json:"UsageData"`
	} `json:"Volumes"`
	BuildCache []struct {
		Size int64 `json:"Size"`
	} `json:"BuildCache"`
}

// dockerStats is one GET /containers/{id}/stats?stream=false sample.
type dockerStats struct {
	MemoryStats struct {
		Usage int64 `json:"usage"`
		Stats struct {
			InactiveFile int64 `json:"inactive_file"`
		} `json:"stats"`
	} `json:"memory_stats"`
	CPUStats    dockerCPUStats `json:"cpu_stats"`
	PreCPUStats dockerCPUStats `json:"precpu_stats"`
}

type dockerCPUStats struct {
	CPUUsage struct {
		TotalUsage  uint64   `json:"total_usage"`
		PercpuUsage []uint64 `json:"percpu_usage"`
	} `json:"cpu_usage"`
	SystemUsage uint64 `json:"system_cpu_usage"`
	OnlineCPUs  int    `json:"online_cpus"`
}

// decodeAnswer decodes one provider answer into its wire type. An empty or null
// answer is the zero value; one of the wrong shape is an error naming what was read.
func decodeAnswer[T any](raw json.RawMessage, what string) (T, error) {
	var out T
	if len(raw) == 0 || string(raw) == "null" {
		return out, nil
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return out, &ProviderError{Message: fmt.Sprintf("decode %s", what), Err: err}
	}
	return out, nil
}
