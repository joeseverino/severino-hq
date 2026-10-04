package providers

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"strconv"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// The dashboard glance: host, container and weather readings on demand. HQ
// shows the text as it is written here.

// missing is application.ui.MISSING.
const missing = "–"

const (
	lookaheadHours = 12
	rainChance     = 30
)

// hostGlanceScript runs on the host with python3 and prints one reading.
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

// GlanceMetric is one line of a panel.
type GlanceMetric struct {
	Label  string `json:"label"`
	Value  string `json:"value"`
	Detail string `json:"detail"`
}

// GlanceMachine is one machine on the infrastructure panel.
type GlanceMachine struct {
	Key           string         `json:"key"`
	Status        string         `json:"status"`
	Summary       string         `json:"summary"`
	Metrics       []GlanceMetric `json:"metrics"`
	RefreshFailed string         `json:"refresh_failed,omitempty"`
}

type infrastructurePanel struct {
	PanelID  string          `json:"panel_id"`
	Machines []GlanceMachine `json:"machines"`
}

// GlanceHour is one hour of the forecast.
type GlanceHour struct {
	Time          string `json:"time"`
	Temperature   string `json:"temperature"`
	Forecast      string `json:"forecast"`
	Precipitation string `json:"precipitation"`
}

type weatherPanel struct {
	PanelID string         `json:"panel_id"`
	Point   string         `json:"point"`
	Status  string         `json:"status"`
	Summary string         `json:"summary"`
	Metrics []GlanceMetric `json:"metrics"`
	Hours   []GlanceHour   `json:"hours"`
}

// weatherFailure marks a weather refresh that failed, so HQ keeps the last good one.
type weatherFailure struct {
	PanelID       string         `json:"panel_id"`
	Point         string         `json:"point"`
	Status        string         `json:"status"`
	Summary       string         `json:"summary"`
	Metrics       []GlanceMetric `json:"metrics"`
	RefreshFailed string         `json:"refresh_failed"`
}

// glanceFailed is the token HQ shows for a failed refresh: the contract's
// failure class when the error carries one.
func glanceFailed(err error) string {
	if failure, _, _ := runtime.Classify(err); failure != runtime.FailureClassUnclassified {
		return string(failure)
	}
	return "unclassified"
}

// humanBytes is application.labels.human_bytes.
func humanBytes(value float64) string {
	amount := math.Max(0, value)
	for _, unit := range []string{"B", "KB", "MB", "GB", "TB"} {
		if amount < 1024 || unit == "TB" {
			if unit == "B" || unit == "KB" || unit == "MB" {
				return fmt.Sprintf("%.0f %s", amount, unit)
			}
			return fmt.Sprintf("%.1f %s", amount, unit)
		}
		amount /= 1024
	}
	return "0 B"
}

func (r *Registry) controllerID() string {
	if r.ControllerID != "" {
		return r.ControllerID
	}
	return r.Env.ControllerID()
}

// Glance reads each panel the plan names. A panel that fails is marked, so HQ
// keeps its last good reading and shows this one as a note.
func (r *Registry) Glance(ctx context.Context, plan runtime.GlancePlan) (runtime.GlanceObservations, error) {
	readings := []any{}
	for _, panel := range plan.Panels {
		var reading any
		var err error
		switch panel {
		case runtime.GlancePanelIDInfrastructure:
			reading, err = r.infrastructureGlance(ctx, plan.Targets.Infrastructure)
		case runtime.GlancePanelIDWeather:
			reading, err = r.weatherGlance(ctx, plan.Targets.Weather)
		default:
			continue
		}
		if err != nil {
			kind := glanceFailed(err)
			slog.Warn(fmt.Sprintf("dashboard glance failed: %s (%s): %s", panel, kind, runtime.Clip(err.Error(), runtime.ReasonLimit)),
				slog.String("event", "controller.glance.failed"), slog.String("panel", string(panel)))
			summary := "Refresh failed (" + kind + ")."
			if panel == runtime.GlancePanelIDInfrastructure {
				reading = infrastructurePanel{PanelID: string(panel), Machines: []GlanceMachine{{
					Key: r.controllerID(), Status: "serious", Summary: summary, Metrics: []GlanceMetric{}, RefreshFailed: kind,
				}}}
			} else {
				reading = weatherFailure{PanelID: string(panel), Point: plan.Targets.Weather.Point, Status: "serious",
					Summary: summary, Metrics: []GlanceMetric{}, RefreshFailed: kind}
			}
		}
		readings = append(readings, reading)
	}
	data, err := json.Marshal(readings)
	if err != nil {
		return nil, err
	}
	observations := runtime.GlanceObservations{}
	return observations, json.Unmarshal(data, &observations)
}

func (r *Registry) infrastructureGlance(ctx context.Context, targets []runtime.GlanceMachineTarget) (infrastructurePanel, error) {
	available := map[string]bool{}
	for _, ref := range r.Env.SSHRefs() {
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
	docker := map[string]GlanceMachine{}
	for _, target := range targets {
		if reachable(target) == "" {
			machines, err := r.portainerGlance(ctx)
			if err != nil {
				return infrastructurePanel{}, err
			}
			for _, machine := range machines {
				docker[machine.Key] = machine
			}
			break
		}
	}
	machines := []GlanceMachine{}
	for _, target := range targets {
		key := strings.TrimSpace(target.Key)
		if ref := reachable(target); ref != "" {
			machine, err := r.hostGlance(ctx, key, ref)
			if err != nil {
				return infrastructurePanel{}, err
			}
			machines = append(machines, machine)
		} else if machine, ok := docker[key]; ok {
			machines = append(machines, machine)
		} else {
			machines = append(machines, GlanceMachine{Key: key, Status: "attention",
				Summary: "No host telemetry connection is available.", Metrics: []GlanceMetric{}})
		}
	}
	return infrastructurePanel{PanelID: string(runtime.GlancePanelIDInfrastructure), Machines: machines}, nil
}

func percent(part, whole int64) float64 {
	if whole == 0 {
		return 0
	}
	return float64(part) / float64(whole) * 100
}

// hostReading is what hostGlanceScript prints.
type hostReading struct {
	CPUPercent   float64 `json:"cpu_percent"`
	Cores        int64   `json:"cores"`
	Load1m       float64 `json:"load_1m"`
	MemoryUsed   int64   `json:"memory_used"`
	MemoryTotal  int64   `json:"memory_total"`
	StorageUsed  int64   `json:"storage_used"`
	StorageTotal int64   `json:"storage_total"`
}

func (r *Registry) hostGlance(ctx context.Context, key, ref string) (GlanceMachine, error) {
	output, err := r.commands().SSH(ctx, ref, "python3 -", []byte(hostGlanceScript))
	if err != nil {
		return GlanceMachine{}, err
	}
	var reading hostReading
	if err := json.Unmarshal(output, &reading); err != nil {
		return GlanceMachine{}, fmt.Errorf("host reading from %s: %w", ref, err)
	}
	return GlanceMachine{
		Key: key, Status: "good", Summary: fmt.Sprintf("Host load %.2f", reading.Load1m),
		Metrics: []GlanceMetric{
			{Label: "CPU", Value: fmt.Sprintf("%.0f%%", reading.CPUPercent), Detail: fmt.Sprintf("%d cores", reading.Cores)},
			{Label: "Memory", Value: fmt.Sprintf("%.0f%%", percent(reading.MemoryUsed, reading.MemoryTotal)),
				Detail: humanBytes(float64(reading.MemoryUsed)) + " of " + humanBytes(float64(reading.MemoryTotal)) + " used"},
			{Label: "Storage", Value: fmt.Sprintf("%.0f%%", percent(reading.StorageUsed, reading.StorageTotal)),
				Detail: humanBytes(float64(reading.StorageUsed)) + " of " + humanBytes(float64(reading.StorageTotal)) + " used on /"},
		},
	}, nil
}

// containerCPUPercent is Docker's share-of-one-core CPU for a stats sample.
func containerCPUPercent(stats dockerStats) float64 {
	cpu, previous := stats.CPUStats, stats.PreCPUStats
	cpuDelta := float64(cpu.CPUUsage.TotalUsage) - float64(previous.CPUUsage.TotalUsage)
	systemDelta := float64(cpu.SystemUsage) - float64(previous.SystemUsage)
	online := float64(cpu.OnlineCPUs)
	if online == 0 {
		online = float64(len(cpu.CPUUsage.PercpuUsage))
	}
	if online == 0 {
		online = 1
	}
	if cpuDelta > 0 && systemDelta > 0 {
		return cpuDelta / systemDelta * online * 100
	}
	return 0
}

// getAnswer GETs address and decodes the answer into T.
func getAnswer[T any](ctx context.Context, r *Registry, address string, headers map[string]string, what string) (T, error) {
	raw, err := r.HTTP.Request(ctx, address, "GET", headers, nil)
	if err != nil {
		var zero T
		return zero, fmt.Errorf("%s: %w", what, err)
	}
	return decodeAnswer[T](raw, what)
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

// portainerGlance is each reachable Portainer environment's containers measured
// against their machine.
func (r *Registry) portainerGlance(ctx context.Context) ([]GlanceMachine, error) {
	refs := r.Env.Refs(runtime.ConnectionProviderPortainer)
	if len(refs) == 0 {
		return nil, &ProviderError{Message: "no Portainer connection was supplied"}
	}
	if r.Portainer == nil {
		return nil, &ProviderError{Message: "Portainer is not available to this controller"}
	}
	machines := []GlanceMachine{}
	for _, ref := range refs {
		base, err := r.Portainer.URL(ref)
		if err != nil {
			return nil, err
		}
		headers, err := r.Portainer.Headers(ref)
		if err != nil {
			return nil, err
		}
		environments, err := r.Portainer.Environments(ctx, ref)
		if err != nil {
			return nil, err
		}
		for _, environment := range environments {
			if !environment.Reachable {
				continue
			}
			machine, err := r.environmentGlance(ctx, base+"/endpoints/"+itoa(environment.ID)+"/docker", headers)
			if err != nil {
				return nil, fmt.Errorf("environment %s: %w", environment.Name, err)
			}
			machine.Key = machineName(environment, r.controllerID())
			machines = append(machines, machine)
		}
	}
	return machines, nil
}

// environmentGlance measures one Docker environment's running containers.
func (r *Registry) environmentGlance(ctx context.Context, docker string, headers map[string]string) (GlanceMachine, error) {
	info, err := getAnswer[dockerInfo](ctx, r, docker+"/info", headers, "docker info")
	if err != nil {
		return GlanceMachine{}, err
	}
	disk, err := getAnswer[dockerDiskUsage](ctx, r, docker+"/system/df", headers, "docker disk usage")
	if err != nil {
		return GlanceMachine{}, err
	}
	containers, err := getAnswer[[]dockerContainer](ctx, r, docker+"/containers/json?all=false", headers, "running containers")
	if err != nil {
		return GlanceMachine{}, err
	}
	var cpu float64
	var memoryUsed int64
	for _, container := range containers {
		if container.ID == "" {
			return GlanceMachine{}, &ProviderError{Message: "running containers: a container has no id"}
		}
		stats, err := getAnswer[dockerStats](ctx, r, docker+"/containers/"+container.ID+"/stats?stream=false&one-shot=true", headers, "container stats")
		if err != nil {
			return GlanceMachine{}, err
		}
		memoryUsed += max(0, stats.MemoryStats.Usage-stats.MemoryStats.Stats.InactiveFile)
		cpu += containerCPUPercent(stats)
	}
	return GlanceMachine{
		Status:  "good",
		Summary: fmt.Sprintf("%d cores · %s memory", info.NCPU, humanBytes(float64(info.MemTotal))),
		Metrics: containerMetrics(int64(len(containers)), cpu, info.NCPU, memoryUsed, info.MemTotal, dockerStorage(disk)),
	}, nil
}

// containerMetrics is what the containers take of their machine. Docker states
// a container's CPU as a share of one core, so the sum is divided by the cores.
func containerMetrics(running int64, cpu float64, cores, memoryUsed, memoryTotal, storage int64) []GlanceMetric {
	machineCPU := cpu
	if cores != 0 {
		machineCPU = cpu / float64(cores)
	}
	return []GlanceMetric{
		{Label: "Containers", Value: strconv.FormatInt(running, 10), Detail: "running"},
		{Label: "CPU", Value: fmt.Sprintf("%.0f%%", machineCPU), Detail: fmt.Sprintf("of %d cores", cores)},
		{Label: "Memory", Value: fmt.Sprintf("%.0f%%", percent(memoryUsed, memoryTotal)),
			Detail: humanBytes(float64(memoryUsed)) + " of " + humanBytes(float64(memoryTotal))},
		{Label: "Docker storage", Value: humanBytes(float64(storage)), Detail: "layers, volumes and build cache"},
	}
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
	Name                       string   `json:"name"`
	StartTime                  string   `json:"startTime"`
	Temperature                *float64 `json:"temperature"`
	TemperatureUnit            string   `json:"temperatureUnit"`
	WindSpeed                  string   `json:"windSpeed"`
	WindDirection              string   `json:"windDirection"`
	ShortForecast              string   `json:"shortForecast"`
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
		} `json:"properties"`
	} `json:"features"`
}

// isoMoment is application.timestamps.moment(stamp, naive="keep").
func isoMoment(stamp string) (time.Time, bool) {
	text := strings.TrimSpace(stamp)
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02 15:04:05.999999999Z07:00",
		"2006-01-02 15:04:05.999999999", "2006-01-02T15:04Z07:00", "2006-01-02T15:04", "2006-01-02"} {
		if found, err := time.Parse(layout, text); err == nil {
			return found, found.Year() != 1 || found.YearDay() != 1
		}
	}
	return time.Time{}, false
}

// hour is the period's start as a 12-hour clock reading, "" when it has none.
func (p nwsPeriod) hour() string {
	found, ok := isoMoment(p.StartTime)
	if !ok {
		return ""
	}
	hour := found.Hour() % 12
	if hour == 0 {
		hour = 12
	}
	meridiem := "PM"
	if found.Hour() < 12 {
		meridiem = "AM"
	}
	return fmt.Sprintf("%d %s", hour, meridiem)
}

// chance is the period's chance of rain in whole percent.
func (p nwsPeriod) chance() int {
	if value := p.ProbabilityOfPrecipitation.Value; value != nil {
		return int(*value)
	}
	return 0
}

// temperature is the period's temperature as written, or missing.
func (p nwsPeriod) temperature() string {
	if p.Temperature == nil {
		return missing
	}
	return degrees(*p.Temperature)
}

func degrees(value float64) string { return strconv.FormatFloat(value, 'f', -1, 64) }

func orDefault(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

// weatherPoint is the configured point as latitude and longitude.
func weatherPoint(raw string) (string, error) {
	parts := strings.Split(strings.TrimSpace(raw), ",")
	if len(parts) != 2 {
		return "", &ProviderError{Message: "SEVERINO_NWS_POINT must be latitude,longitude"}
	}
	latitude, errLat := strconv.ParseFloat(strings.TrimSpace(parts[0]), 64)
	longitude, errLon := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
	if errLat != nil || errLon != nil {
		return "", &ProviderError{Message: "SEVERINO_NWS_POINT is not numeric"}
	}
	if !(latitude >= -90 && latitude <= 90) || !(longitude >= -180 && longitude <= 180) {
		return "", &ProviderError{Message: "SEVERINO_NWS_POINT is outside valid coordinates"}
	}
	return fmt.Sprintf("%.4f,%.4f", latitude, longitude), nil
}

// weatherGlance reads the forecast for the plan's point from the API the plan
// names: HQ states the National Weather Service's address once.
func (r *Registry) weatherGlance(ctx context.Context, target runtime.GlanceWeatherTarget) (weatherPanel, error) {
	located, err := weatherPoint(target.Point)
	if err != nil {
		return weatherPanel{}, err
	}
	api := strings.TrimRight(strings.TrimSpace(target.Endpoint), "/")
	if api == "" {
		return weatherPanel{}, &ProviderError{Message: "the glance plan names no weather API"}
	}
	headers := map[string]string{"Accept": "application/geo+json", "User-Agent": nwsUserAgent}
	point, err := getAnswer[nwsPoint](ctx, r, api+"/points/"+located, headers, "weather point")
	if err != nil {
		return weatherPanel{}, err
	}
	forecast, err := getAnswer[nwsForecast](ctx, r, point.Properties.ForecastHourly, headers, "hourly forecast")
	if err != nil {
		return weatherPanel{}, err
	}
	periods := forecast.Properties.Periods
	if len(periods) > lookaheadHours {
		periods = periods[:lookaheadHours]
	}
	var current nwsPeriod
	if len(periods) > 0 {
		current = periods[0]
	}
	alerts, err := getAnswer[nwsAlerts](ctx, r, api+"/alerts/active?point="+located, headers, "weather alerts")
	if err != nil {
		return weatherPanel{}, err
	}
	place := point.Properties.RelativeLocation.Properties
	status := "good"
	if len(alerts.Features) > 0 {
		status = "serious"
	}
	metrics := []GlanceMetric{
		{Label: "Now", Value: orDefault(current.ShortForecast, "Unavailable"), Detail: current.Name},
		{Label: "Temperature", Value: current.temperature() + "°" + orDefault(current.TemperatureUnit, "F")},
	}
	metrics = append(metrics, outlook(periods)...)
	metrics = append(metrics, GlanceMetric{Label: "Wind",
		Value:  strings.TrimSpace(current.WindDirection + " " + orDefault(current.WindSpeed, missing)),
		Detail: "NWS hourly forecast"})
	metrics = append(metrics, alertMetric(alerts)...)
	hours := []GlanceHour{}
	for _, period := range periods {
		hours = append(hours, GlanceHour{
			Time: period.hour(), Temperature: period.temperature() + "°",
			Forecast: period.ShortForecast, Precipitation: fmt.Sprintf("%d%%", period.chance()),
		})
	}
	summary := strings.Join(nonEmpty([]string{place.City, place.State}), ", ")
	return weatherPanel{PanelID: string(runtime.GlancePanelIDWeather), Point: located, Status: status,
		Summary: orDefault(summary, "National Weather Service"), Metrics: metrics, Hours: hours}, nil
}

// outlook is the range ahead and when rain next comes, from the hourly periods.
func outlook(periods []nwsPeriod) []GlanceMetric {
	found := []GlanceMetric{}
	var low, high *float64
	for _, period := range periods {
		if t := period.Temperature; t != nil {
			if low == nil || *t < *low {
				low = t
			}
			if high == nil || *t > *high {
				high = t
			}
		}
	}
	if low != nil {
		found = append(found, GlanceMetric{Label: "Range", Value: degrees(*low) + "–" + degrees(*high) + "°",
			Detail: fmt.Sprintf("next %d hours", len(periods))})
	}
	for i, period := range periods {
		if period.chance() < rainChance {
			continue
		}
		when := period.hour()
		if i == 0 {
			when = "Now"
		}
		found = append(found, GlanceMetric{Label: "Rain", Value: fmt.Sprintf("%s · %d%%", when, period.chance()),
			Detail: period.ShortForecast})
		break
	}
	return found
}

// alertMetric is the active alert by name, with how many more there are.
func alertMetric(alerts nwsAlerts) []GlanceMetric {
	if len(alerts.Features) == 0 {
		return []GlanceMetric{}
	}
	first := alerts.Features[0].Properties
	more := ""
	if n := len(alerts.Features); n > 1 {
		more = fmt.Sprintf(" +%d", n-1)
	}
	return []GlanceMetric{{Label: "Alerts", Value: orDefault(first.Event, "Weather alert") + more, Detail: first.Headline}}
}
