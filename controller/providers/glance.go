package providers

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// The dashboard glance: host, container and weather readings on demand,
// reported as the numbers, instants and states they are. HQ words them.

// How many hours of the forecast, and how many alerts, a reading carries: as
// many as the contract takes.
var (
	lookaheadHours = api.MustLimit("GlanceObservationWeather", "properties", "hours", "maxItems")
	alertsLimit    = api.MustLimit("GlanceObservationWeather", "properties", "alerts", "maxItems")
)

// hostGlanceScript runs on the host with python3 and prints one
// runtime.GlanceObservationHost.
const hostGlanceScript = `import json
import os
import shutil
import time

def cpu_reading():
    with open('/proc/stat', encoding='utf-8') as source:
        values = [int(value) for value in source.readline().split()[1:]]
    return sum(values), values[3] + values[4]

before_total, before_idle = cpu_reading()
time.sleep(0.2)
after_total, after_idle = cpu_reading()
elapsed = after_total - before_total
cpu = 100 * (1 - ((after_idle - before_idle) / elapsed)) if elapsed else 0
memory = {}
with open('/proc/meminfo', encoding='utf-8') as source:
    for line in source:
        key, value = line.split(':', 1)
        memory[key] = int(value.split()[0]) * 1024
total_memory = memory.get('MemTotal', 0)
available_memory = memory.get('MemAvailable', memory.get('MemFree', 0))
disk = shutil.disk_usage('/')
print(json.dumps({
    'cpu_percent': cpu,
    'cores': os.cpu_count() or 0,
    'load_1m': os.getloadavg()[0],
    'memory_used': max(0, total_memory - available_memory),
    'memory_total': total_memory,
    'storage_used': disk.used,
    'storage_total': disk.total,
}))
`

// PortainerSource is the Portainer access the glance needs: where a connection's
// API is, the headers it takes, and the environments it manages.
type PortainerSource interface {
	URL(ref string) (string, error)
	Headers(ref string) (map[string]string, error)
	Environments(ctx context.Context, ref string) ([]PortainerEnvironment, error)
}

// glanceFailed is the token HQ shows for a failed refresh: the contract's
// failure class when the error carries one.
func glanceFailed(err error) string {
	if failure, _, _ := runtime.Classify(err); failure != runtime.FailureClassUnclassified {
		return string(failure)
	}
	return "unclassified"
}

func (r *Registry) controllerID() string {
	if r.ControllerID != "" {
		return r.ControllerID
	}
	return r.Env.ControllerID()
}

// glanceStatsLimit bounds the container samples in flight against one Docker
// environment: enough that a machine's containers are measured together, few
// enough that the glance is a small load on the API its containers share.
const glanceStatsLimit = 8

// timedStep runs one step of the glance and logs how long it took, so the
// journal says where a slow refresh spent its time. A call is one request to a
// provider; a stage is a panel, a machine or an environment.
func timedStep[T any](kind, step string, read func() (T, error)) (T, error) {
	start := time.Now()
	value, err := read()
	attrs := []any{slog.String("event", "controller.glance."+kind), slog.String("step", step),
		slog.Int64("duration_ms", time.Since(start).Milliseconds())}
	if err != nil {
		attrs = append(attrs, slog.String("failure", glanceFailed(err)))
	}
	slog.Info("dashboard glance "+kind, attrs...)
	return value, err
}

// Glance reads each panel the plan names, the panels at once. A panel that
// fails is marked, so HQ keeps its last good reading and shows this one as a
// note.
func (r *Registry) Glance(ctx context.Context, plan runtime.GlancePlan) (runtime.GlanceObservations, error) {
	return timedStep("stage", "glance", func() (runtime.GlanceObservations, error) {
		readings := make([]*runtime.GlanceObservation, len(plan.Panels))
		var group sync.WaitGroup
		for i, panel := range plan.Panels {
			group.Go(func() { readings[i] = r.glancePanel(ctx, plan, panel) })
		}
		group.Wait()
		observations := runtime.GlanceObservations{}
		for _, reading := range readings {
			if reading != nil {
				observations = append(observations, *reading)
			}
		}
		return observations, nil
	})
}

// glancePanel is one panel's reading, or its failure; nil for a panel this
// controller does not read.
func (r *Registry) glancePanel(ctx context.Context, plan runtime.GlancePlan, panel runtime.GlancePanelID) *runtime.GlanceObservation {
	reading := &runtime.GlanceObservation{PanelID: panel}
	var err error
	switch panel {
	case runtime.GlancePanelIDInfrastructure:
		reading.Machines, err = timedStep("stage", string(panel), func() ([]runtime.GlanceObservationMachine, error) {
			return r.infrastructureGlance(ctx, plan.Targets.Infrastructure)
		})
	case runtime.GlancePanelIDWeather:
		reading.Weather, err = timedStep("stage", string(panel), func() (*runtime.GlanceObservationWeather, error) {
			return r.weatherGlance(ctx, plan.Targets.Weather)
		})
	default:
		return nil
	}
	if err == nil {
		return reading
	}
	kind := glanceFailed(err)
	slog.Warn(fmt.Sprintf("dashboard glance failed: %s (%s): %s", panel, kind, runtime.Clip(err.Error(), runtime.ReasonLimit)),
		slog.String("event", "controller.glance.failed"), slog.String("panel", string(panel)))
	if panel == runtime.GlancePanelIDInfrastructure {
		reading.Machines = []runtime.GlanceObservationMachine{{Key: r.controllerID(), RefreshFailed: kind}}
	} else {
		reading.Weather = &runtime.GlanceObservationWeather{Point: plan.Targets.Weather.Point, RefreshFailed: kind}
	}
	return reading
}

// infrastructureGlance reads every machine at once: one over its own SSH
// connection, the rest through Portainer, which reads only the environments
// that are those machines. A machine neither reaches has no reading.
func (r *Registry) infrastructureGlance(ctx context.Context, targets []runtime.GlanceMachineTarget) ([]runtime.GlanceObservationMachine, error) {
	available := map[string]bool{}
	for _, ref := range r.Supplied.SSHRefs() {
		available[ref] = true
	}
	reachable := func(target runtime.GlanceMachineTarget) string {
		for _, ref := range target.Connections {
			if available[ref] {
				return ref
			}
		}
		return ""
	}
	throughDocker := map[string]bool{}
	for _, target := range targets {
		if reachable(target) == "" {
			throughDocker[strings.TrimSpace(target.Key)] = true
		}
	}
	group, groupCtx := errgroup.WithContext(ctx)
	docker := map[string]runtime.GlanceObservationMachine{}
	if len(throughDocker) > 0 {
		group.Go(func() error {
			machines, err := r.portainerGlance(groupCtx, throughDocker)
			for _, machine := range machines {
				docker[machine.Key] = machine
			}
			return err
		})
	}
	machines := make([]runtime.GlanceObservationMachine, len(targets))
	for i, target := range targets {
		key := strings.TrimSpace(target.Key)
		if ref := reachable(target); ref != "" {
			group.Go(func() (err error) {
				machines[i], err = timedStep("stage", "machine "+key, func() (runtime.GlanceObservationMachine, error) {
					return r.hostGlance(groupCtx, key, ref)
				})
				return err
			})
		}
	}
	if err := group.Wait(); err != nil {
		return nil, err
	}
	for i, target := range targets {
		key := strings.TrimSpace(target.Key)
		if throughDocker[key] {
			machines[i] = docker[key]
			machines[i].Key = key
		}
	}
	return machines, nil
}

func (r *Registry) hostGlance(ctx context.Context, key, ref string) (runtime.GlanceObservationMachine, error) {
	output, err := timedStep("call", "host reading", func() ([]byte, error) {
		return r.commands().SSH(ctx, ref, "python3 -", []byte(hostGlanceScript))
	})
	if err != nil {
		return runtime.GlanceObservationMachine{}, err
	}
	var reading runtime.GlanceObservationHost
	if err := json.Unmarshal(output, &reading); err != nil {
		return runtime.GlanceObservationMachine{}, fmt.Errorf("host reading from %s: %w", ref, err)
	}
	return runtime.GlanceObservationMachine{Key: key, Host: &reading}, nil
}

// getAnswer GETs address and decodes the answer into T, logging the call's time.
func getAnswer[T any](ctx context.Context, r *Registry, address string, headers map[string]string, what string) (T, error) {
	return timedStep("call", what, func() (T, error) {
		raw, err := r.HTTP.Request(ctx, address, "GET", headers, nil)
		if err != nil {
			var zero T
			return zero, fmt.Errorf("%s: %w", what, err)
		}
		return decodeAnswer[T](raw, what)
	})
}

// dockerStorage is what Docker holds on disk: layers, volumes and build cache.
// A size Docker did not compute (-1) counts as nothing.
func dockerStorage(disk dockerDiskUsage) int64 {
	total := max(disk.LayersSize, 0)
	for _, volume := range disk.Volumes {
		if volume.UsageData != nil {
			total += max(volume.UsageData.Size, 0)
		}
	}
	for _, cache := range disk.BuildCache {
		total += max(cache.Size, 0)
	}
	return total
}

// portainerGlance measures the containers of each reachable Portainer
// environment that is one of the wanted machines, the environments at once.
func (r *Registry) portainerGlance(ctx context.Context, wanted map[string]bool) ([]runtime.GlanceObservationMachine, error) {
	refs := r.Supplied.Refs(runtime.ConnectionProviderPortainer)
	if len(refs) == 0 {
		return nil, &ProviderError{Message: "no Portainer connection was supplied"}
	}
	if r.Portainer == nil {
		return nil, &ProviderError{Message: "Portainer is not connected to HQ on this host"}
	}
	var mu sync.Mutex
	machines := []runtime.GlanceObservationMachine{}
	group, groupCtx := errgroup.WithContext(ctx)
	for _, ref := range refs {
		base, err := r.Portainer.URL(ref)
		if err != nil {
			return nil, err
		}
		headers, err := r.Portainer.Headers(ref)
		if err != nil {
			return nil, err
		}
		group.Go(func() error {
			environments, err := timedStep("call", "environment list", func() ([]PortainerEnvironment, error) {
				return r.Portainer.Environments(groupCtx, ref)
			})
			if err != nil {
				return err
			}
			for _, environment := range environments {
				key := machineName(environment, r.controllerID())
				if !environment.Reachable || !wanted[key] {
					continue
				}
				group.Go(func() error {
					machine, err := timedStep("stage", "environment "+environment.Name, func() (runtime.GlanceObservationMachine, error) {
						return r.environmentGlance(groupCtx, base+"/endpoints/"+itoa(environment.ID)+"/docker", headers)
					})
					if err != nil {
						return fmt.Errorf("environment %s: %w", environment.Name, err)
					}
					machine.Key = key
					mu.Lock()
					machines = append(machines, machine)
					mu.Unlock()
					return nil
				})
			}
			return nil
		})
	}
	if err := group.Wait(); err != nil {
		return nil, err
	}
	return machines, nil
}

// glanceDiskUsage asks Docker for the sizes the storage reading adds up and no
// others: without a type, Docker also measures each container's writable layer.
const glanceDiskUsage = "/system/df?type=image&type=volume&type=build-cache"

// glanceCPUWindow is the shortest time between a container's two samples, the
// window its CPU share is taken over: the host glance's own.
const glanceCPUWindow = 200 * time.Millisecond

// sampleContainers takes one one-shot sample of each container,
// glanceStatsLimit at a time, in the containers' order.
func (r *Registry) sampleContainers(ctx context.Context, docker string, headers map[string]string, containers []dockerContainer) ([]dockerStats, error) {
	samples := make([]dockerStats, len(containers))
	stats, statsCtx := errgroup.WithContext(ctx)
	stats.SetLimit(glanceStatsLimit)
	for i, container := range containers {
		stats.Go(func() (err error) {
			samples[i], err = getAnswer[dockerStats](statsCtx, r, docker+oneShotStats(container.ID), headers, "container stats")
			return err
		})
	}
	return samples, stats.Wait()
}

// waitFor returns after d, or with the context's error when it ends first.
func waitFor(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// environmentGlance measures one Docker environment's running containers: the
// machine, its storage and the container list at once, then two samples of
// each container, glanceCPUWindow apart, for its CPU over that window.
func (r *Registry) environmentGlance(ctx context.Context, docker string, headers map[string]string) (runtime.GlanceObservationMachine, error) {
	var info dockerInfo
	var disk dockerDiskUsage
	var containers []dockerContainer
	group, groupCtx := errgroup.WithContext(ctx)
	group.Go(func() (err error) {
		info, err = getAnswer[dockerInfo](groupCtx, r, docker+"/info", headers, "docker info")
		return err
	})
	group.Go(func() (err error) {
		disk, err = getAnswer[dockerDiskUsage](groupCtx, r, docker+glanceDiskUsage, headers, "docker disk usage")
		return err
	})
	containers, err := getAnswer[[]dockerContainer](groupCtx, r, docker+"/containers/json?all=false", headers, "running containers")
	if err != nil {
		_ = group.Wait()
		return runtime.GlanceObservationMachine{}, err
	}
	for _, container := range containers {
		if container.ID == "" {
			_ = group.Wait()
			return runtime.GlanceObservationMachine{}, &ProviderError{Message: "running containers: a container has no id"}
		}
	}
	first, statsErr := r.sampleContainers(groupCtx, docker, headers, containers)
	var second []dockerStats
	if statsErr == nil {
		statsErr = waitFor(groupCtx, glanceCPUWindow)
	}
	if statsErr == nil {
		second, statsErr = r.sampleContainers(groupCtx, docker, headers, containers)
	}
	if err := group.Wait(); err != nil {
		return runtime.GlanceObservationMachine{}, err
	}
	if statsErr != nil {
		return runtime.GlanceObservationMachine{}, statsErr
	}
	var cpu float64
	var memoryUsed int64
	for i, sample := range second {
		used, _ := memoryInUse(sample)
		memoryUsed += used
		if share, ok := cpuShare(first[i], sample); ok {
			cpu += share
		}
	}
	// Docker states a container's CPU as a share of one core, so the sum is
	// divided by the cores for the machine's share.
	if info.NCPU != 0 {
		cpu /= float64(info.NCPU)
	}
	return runtime.GlanceObservationMachine{Docker: &runtime.GlanceObservationDocker{
		Containers: len(containers), CPUPercent: cpu, Cores: int(info.NCPU),
		MemoryUsed: int(memoryUsed), MemoryTotal: int(info.MemTotal), Storage: int(dockerStorage(disk)),
	}}, nil
}

// nwsUserAgent is the contact NWS asks every client to send.
const nwsUserAgent = "Severino-HQ/1.0 (https://github.com/joeseverino/severino-hq)"

// nwsPoint is GET /points/{lat},{lon}.
type nwsPoint struct {
	Properties struct {
		ForecastHourly   string `json:"forecastHourly"`
		RelativeLocation struct {
			Properties struct {
				City  string `json:"city"`
				State string `json:"state"`
			} `json:"properties"`
		} `json:"relativeLocation"`
	} `json:"properties"`
}

// nwsForecast is the hourly forecast the point names.
type nwsForecast struct {
	Properties struct {
		Periods []nwsPeriod `json:"periods"`
	} `json:"properties"`
}

type nwsPeriod struct {
	Name                       string    `json:"name"`
	StartTime                  time.Time `json:"startTime"`
	Temperature                *float64  `json:"temperature"`
	TemperatureUnit            string    `json:"temperatureUnit"`
	WindSpeed                  string    `json:"windSpeed"`
	WindDirection              string    `json:"windDirection"`
	ShortForecast              string    `json:"shortForecast"`
	ProbabilityOfPrecipitation struct {
		Value *float64 `json:"value"`
	} `json:"probabilityOfPrecipitation"`
}

// nwsAlerts is GET /alerts/active.
type nwsAlerts struct {
	Features []struct {
		Properties struct {
			Event    string `json:"event"`
			Headline string `json:"headline"`
			Severity string `json:"severity"`
		} `json:"properties"`
	} `json:"features"`
}

// weatherPoint is the configured point as latitude and longitude.
func weatherPoint(raw string) (string, error) {
	parts := strings.Split(strings.TrimSpace(raw), ",")
	if len(parts) != 2 {
		return "", &ProviderError{Message: "the weather location must be latitude,longitude"}
	}
	latitude, errLat := strconv.ParseFloat(strings.TrimSpace(parts[0]), 64)
	longitude, errLon := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
	if errLat != nil || errLon != nil {
		return "", &ProviderError{Message: "the weather location is not numeric"}
	}
	if !(latitude >= -90 && latitude <= 90) || !(longitude >= -180 && longitude <= 180) {
		return "", &ProviderError{Message: "the weather location is outside valid coordinates"}
	}
	return fmt.Sprintf("%.4f,%.4f", latitude, longitude), nil
}

// weatherGlance reads the forecast for the plan's point from the API the plan
// names: HQ states the National Weather Service's address once.
func (r *Registry) weatherGlance(ctx context.Context, target runtime.GlanceWeatherTarget) (*runtime.GlanceObservationWeather, error) {
	located, err := weatherPoint(target.Point)
	if err != nil {
		return nil, err
	}
	endpoint := strings.TrimRight(strings.TrimSpace(target.Endpoint), "/")
	if endpoint == "" {
		return nil, &ProviderError{Message: "the glance plan names no weather API"}
	}
	headers := map[string]string{"Accept": "application/geo+json", "User-Agent": nwsUserAgent}
	// The alerts need only the point, so they are read while the forecast is.
	var alerts nwsAlerts
	alertsRead := make(chan error, 1)
	go func() {
		var err error
		alerts, err = getAnswer[nwsAlerts](ctx, r, endpoint+"/alerts/active?point="+located, headers, "weather alerts")
		alertsRead <- err
	}()
	point, err := getAnswer[nwsPoint](ctx, r, endpoint+"/points/"+located, headers, "weather point")
	var forecast nwsForecast
	if err == nil {
		forecast, err = getAnswer[nwsForecast](ctx, r, point.Properties.ForecastHourly, headers, "hourly forecast")
	}
	if alertsErr := <-alertsRead; err == nil {
		err = alertsErr
	}
	if err != nil {
		return nil, err
	}
	periods := forecast.Properties.Periods
	if len(periods) > lookaheadHours {
		periods = periods[:lookaheadHours]
	}
	place := point.Properties.RelativeLocation.Properties
	reading := &runtime.GlanceObservationWeather{Point: located, City: place.City, State: place.State,
		Hours:  make([]runtime.GlanceObservationHour, 0, len(periods)),
		Alerts: make([]runtime.GlanceObservationAlert, 0, alertsLimit)}
	for _, period := range periods {
		chance := 0
		if value := period.ProbabilityOfPrecipitation.Value; value != nil {
			chance = int(*value)
		}
		reading.Hours = append(reading.Hours, runtime.GlanceObservationHour{
			Start: period.StartTime, Name: period.Name, Temperature: period.Temperature,
			TemperatureUnit: period.TemperatureUnit, Forecast: period.ShortForecast, Chance: chance,
			WindSpeed: period.WindSpeed, WindDirection: period.WindDirection,
		})
	}
	for _, feature := range alerts.Features[:min(len(alerts.Features), alertsLimit)] {
		alert := feature.Properties
		reading.Alerts = append(reading.Alerts, runtime.GlanceObservationAlert{
			Event: alert.Event, Headline: alert.Headline, Severity: alert.Severity,
		})
	}
	return reading, nil
}
