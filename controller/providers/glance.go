package providers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"strconv"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// The dashboard glance: host, container and weather readings on demand. Text is
// formatted as the Python controller formats it, because HQ shows it verbatim.

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

// pyFailure names a failure as the Python glance reports it: the exception's type.
type pyFailure struct {
	kind    string
	message string
}

func (e *pyFailure) Error() string { return e.message }

func failureKind(err error) string {
	var named *pyFailure
	if errors.As(err, &named) {
		return named.kind
	}
	return "ProviderError"
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

// pyFloat is float(v or 0) for a decoded value.
func pyFloat(v *pyValue) (float64, error) {
	if v == nil || !v.truthy() {
		return 0, nil
	}
	if v.text != nil {
		f, err := strconv.ParseFloat(strings.TrimSpace(*v.text), 64)
		if err != nil {
			return 0, &pyFailure{"ValueError", "could not convert string to float"}
		}
		return f, nil
	}
	if n, ok := v.number(); ok {
		if n.inf != 0 {
			return math.Inf(n.inf), nil
		}
		f, _ := n.value.Float64()
		return f, nil
	}
	return 0, &pyFailure{"TypeError", "float() argument must be a string or a real number"}
}

// pyInt is int(v or 0) for a decoded value: a float truncates.
func pyInt(v *pyValue) (int64, error) {
	if v == nil || !v.truthy() {
		return 0, nil
	}
	if v.text != nil {
		i, err := strconv.ParseInt(strings.TrimSpace(*v.text), 10, 64)
		if err != nil {
			return 0, &pyFailure{"ValueError", "invalid literal for int()"}
		}
		return i, nil
	}
	f, err := pyFloat(v)
	return int64(f), err
}

func field(v *pyValue, key string) *pyValue {
	if v == nil {
		return nil
	}
	return v.get(key).opt()
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
			reading, err = r.weatherGlance(ctx, plan.Targets.Weather.Point)
		default:
			continue
		}
		if err != nil {
			kind := failureKind(err)
			slog.Warn(fmt.Sprintf("dashboard glance failed: %s (%s): %s", string(panel), kind, runtime.Clip(err.Error(), runtime.ReasonLimit)),
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

func (r *Registry) hostGlance(ctx context.Context, key, ref string) (GlanceMachine, error) {
	output, err := r.commands().SSH(ctx, ref, "python3 -", []byte(hostGlanceScript))
	if err != nil {
		return GlanceMachine{}, err
	}
	reading, err := parsePy(output)
	if err != nil {
		return GlanceMachine{}, &pyFailure{"JSONDecodeError", "The host reading is not JSON."}
	}
	if !reading.object {
		return GlanceMachine{}, &pyFailure{"AttributeError", "The host reading is not an object."}
	}
	ints := map[string]int64{}
	for _, name := range []string{"memory_used", "memory_total", "storage_used", "storage_total", "cores"} {
		if ints[name], err = pyInt(reading.get(name).opt()); err != nil {
			return GlanceMachine{}, err
		}
	}
	load, err := pyFloat(reading.get("load_1m").opt())
	if err != nil {
		return GlanceMachine{}, err
	}
	cpu, err := pyFloat(reading.get("cpu_percent").opt())
	if err != nil {
		return GlanceMachine{}, err
	}
	return GlanceMachine{
		Key: key, Status: "good", Summary: fmt.Sprintf("Host load %.2f", load),
		Metrics: []GlanceMetric{
			{Label: "CPU", Value: fmt.Sprintf("%.0f%%", cpu), Detail: fmt.Sprintf("%d cores", ints["cores"])},
			{Label: "Memory", Value: fmt.Sprintf("%.0f%%", percent(ints["memory_used"], ints["memory_total"])),
				Detail: humanBytes(float64(ints["memory_used"])) + " of " + humanBytes(float64(ints["memory_total"])) + " used"},
			{Label: "Storage", Value: fmt.Sprintf("%.0f%%", percent(ints["storage_used"], ints["storage_total"])),
				Detail: humanBytes(float64(ints["storage_used"])) + " of " + humanBytes(float64(ints["storage_total"])) + " used on /"},
		},
	}, nil
}

// containerCPUPercent is Docker's share-of-one-core CPU for a stats sample.
func containerCPUPercent(stats *pyValue) float64 {
	cpu, previous := field(stats, "cpu_stats"), field(stats, "precpu_stats")
	f := func(v *pyValue) float64 { x, _ := pyFloat(v); return x }
	cpuDelta := f(field(field(cpu, "cpu_usage"), "total_usage")) - f(field(field(previous, "cpu_usage"), "total_usage"))
	systemDelta := f(field(cpu, "system_cpu_usage")) - f(field(previous, "system_cpu_usage"))
	online := f(field(cpu, "online_cpus"))
	if online == 0 {
		if percpu := field(field(cpu, "cpu_usage"), "percpu_usage"); percpu != nil {
			online = float64(len(percpu.array))
		}
	}
	if online == 0 {
		online = 1
	}
	if cpuDelta > 0 && systemDelta > 0 {
		return cpuDelta / systemDelta * online * 100
	}
	return 0
}

func (r *Registry) getJSON(ctx context.Context, address string, headers map[string]string) (pyValue, error) {
	raw, err := r.HTTP.Request(ctx, address, "GET", headers, nil)
	if err != nil {
		return pyValue{}, err
	}
	if len(raw) == 0 {
		return pyValue{literal: "null"}, nil
	}
	return parsePy(raw)
}

// portainerGlance is each reachable Portainer environment's containers measured
// against their machine.
func (r *Registry) portainerGlance(ctx context.Context) ([]GlanceMachine, error) {
	refs := r.Env.Refs("portainer")
	if len(refs) == 0 {
		return nil, &ProviderError{Message: "No Portainer connection was supplied."}
	}
	if r.Portainer == nil {
		return nil, &ProviderError{Message: "Portainer is not available to this controller."}
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
			prefix := base + "/endpoints/" + environment.ID.str() + "/docker"
			info, err := r.getJSON(ctx, prefix+"/info", headers)
			if err != nil {
				return nil, err
			}
			disk, err := r.getJSON(ctx, prefix+"/system/df", headers)
			if err != nil {
				return nil, err
			}
			containers, err := r.getJSON(ctx, prefix+"/containers/json?all=false", headers)
			if err != nil {
				return nil, err
			}
			cores, err := pyInt(info.get("NCPU").opt())
			if err != nil {
				return nil, err
			}
			memoryTotal, err := pyInt(info.get("MemTotal").opt())
			if err != nil {
				return nil, err
			}
			storage, err := pyInt(disk.get("LayersSize").opt())
			if err != nil {
				return nil, err
			}
			for _, list := range []string{"Volumes", "BuildCache"} {
				items := disk.get(list).opt()
				if items == nil {
					continue
				}
				for i := range items.array {
					size := field(&items.array[i], "Size")
					if list == "Volumes" {
						size = field(field(&items.array[i], "UsageData"), "Size")
					}
					n, err := pyInt(size)
					if err != nil {
						return nil, err
					}
					storage += n
				}
			}
			var cpu float64
			var memoryUsed, running int64
			for i := range containers.array {
				id := containers.array[i].get("Id").opt()
				if id == nil {
					return nil, &pyFailure{"KeyError", "Id"}
				}
				stats, err := r.getJSON(ctx, prefix+"/containers/"+id.str()+"/stats?stream=false&one-shot=true", headers)
				if err != nil {
					return nil, err
				}
				memory := field(&stats, "memory_stats")
				cache, err := pyInt(field(field(memory, "stats"), "inactive_file"))
				if err != nil {
					return nil, err
				}
				usage, err := pyInt(field(memory, "usage"))
				if err != nil {
					return nil, err
				}
				memoryUsed += max(0, usage-cache)
				cpu += containerCPUPercent(&stats)
				running++
			}
			key := environment.Name
			if environment.Local && r.controllerID() != "" {
				key = r.controllerID()
			}
			machines = append(machines, GlanceMachine{
				Key: key, Status: "good",
				Summary: fmt.Sprintf("%d cores · %s memory", cores, humanBytes(float64(memoryTotal))),
				Metrics: containerMetrics(running, cpu, cores, memoryUsed, memoryTotal, storage),
			})
		}
	}
	return machines, nil
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

func periodHour(period *pyValue) string {
	start := field(period, "startTime")
	if start == nil {
		return ""
	}
	found, ok := isoMoment(start.str())
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

func periodChance(period *pyValue) int64 {
	value := field(field(period, "probabilityOfPrecipitation"), "value")
	if value == nil || value.text != nil || value.object || value.array != nil || value.literal == "null" {
		return 0
	}
	n, _ := pyInt(value)
	return n
}

// pyFormatOr is f"{d.get(key, fallback)}".
func pyFormatOr(v *pyValue, key, fallback string) string {
	if found := field(v, key); found != nil {
		return found.str()
	}
	return fallback
}

func (r *Registry) weatherGlance(ctx context.Context, rawPoint string) (weatherPanel, error) {
	point := strings.TrimSpace(rawPoint)
	parts := strings.Split(point, ",")
	if len(parts) != 2 {
		return weatherPanel{}, &ProviderError{Message: "SEVERINO_NWS_POINT must be latitude,longitude."}
	}
	latitude, errLat := strconv.ParseFloat(strings.TrimSpace(parts[0]), 64)
	longitude, errLon := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
	if errLat != nil || errLon != nil {
		return weatherPanel{}, &ProviderError{Message: "SEVERINO_NWS_POINT is not numeric."}
	}
	if !(latitude >= -90 && latitude <= 90) || !(longitude >= -180 && longitude <= 180) {
		return weatherPanel{}, &ProviderError{Message: "SEVERINO_NWS_POINT is outside valid coordinates."}
	}
	headers := map[string]string{
		"Accept":     "application/geo+json",
		"User-Agent": "Severino-HQ/1.0 (https://github.com/joeseverino/severino-hq)",
	}
	located := fmt.Sprintf("%.4f,%.4f", latitude, longitude)
	pointData, err := r.getJSON(ctx, "https://api.weather.gov/points/"+located, headers)
	if err != nil {
		return weatherPanel{}, err
	}
	properties := objectField(&pointData, "properties")
	hourly, err := r.getJSON(ctx, strField(properties, "forecastHourly"), headers)
	if err != nil {
		return weatherPanel{}, err
	}
	periods := []*pyValue{}
	if listed := objectField(objectField(&hourly, "properties"), "periods"); listed != nil {
		for i := range listed.array {
			if listed.array[i].object && len(periods) < lookaheadHours {
				periods = append(periods, &listed.array[i])
			}
		}
	}
	current := &pyValue{object: true}
	if len(periods) > 0 {
		current = periods[0]
	}
	alerts, err := r.getJSON(ctx, "https://api.weather.gov/alerts/active?point="+located, headers)
	if err != nil {
		return weatherPanel{}, err
	}
	active := []pyValue{}
	if features := objectField(&alerts, "features"); features != nil {
		active = features.array
	}
	place := objectField(objectField(properties, "relativeLocation"), "properties")
	location := []string{}
	for _, key := range []string{"city", "state"} {
		if part := field(place, key); part != nil && part.truthy() {
			location = append(location, part.str())
		}
	}
	status := "good"
	if len(active) > 0 {
		status = "serious"
	}
	now := strField(current, "shortForecast")
	if now == "" {
		now = "Unavailable"
	}
	metrics := []GlanceMetric{
		{Label: "Now", Value: now, Detail: strField(current, "name")},
		{Label: "Temperature", Value: pyFormatOr(current, "temperature", missing) + "°" + pyFormatOr(current, "temperatureUnit", "F"),
			Detail: strField(current, "windChill")},
	}
	metrics = append(metrics, outlook(periods)...)
	metrics = append(metrics, GlanceMetric{Label: "Wind",
		Value:  strings.TrimSpace(pyFormatOr(current, "windDirection", "") + " " + pyFormatOr(current, "windSpeed", missing)),
		Detail: "NWS hourly forecast"})
	metrics = append(metrics, alertMetric(active)...)
	summary := strings.Join(location, ", ")
	if summary == "" {
		summary = "National Weather Service"
	}
	hours := []GlanceHour{}
	for _, period := range periods {
		hours = append(hours, GlanceHour{
			Time: periodHour(period), Temperature: pyFormatOr(period, "temperature", missing) + "°",
			Forecast: strField(period, "shortForecast"), Precipitation: fmt.Sprintf("%d%%", periodChance(period)),
		})
	}
	return weatherPanel{PanelID: string(runtime.GlancePanelIDWeather), Point: located, Status: status, Summary: summary, Metrics: metrics, Hours: hours}, nil
}

// outlook is the range ahead and when rain next comes, from the hourly periods.
func outlook(periods []*pyValue) []GlanceMetric {
	found := []GlanceMetric{}
	var low, high *pyValue
	var lowN, highN pyNum
	for _, period := range periods {
		temperature := period.get("temperature").opt()
		if temperature == nil || temperature.text != nil || temperature.object || temperature.array != nil || temperature.literal == "null" {
			continue
		}
		n, ok := temperature.number()
		if !ok {
			continue
		}
		if low == nil || n.less(lowN) {
			low, lowN = temperature, n
		}
		if high == nil || highN.less(n) {
			high, highN = temperature, n
		}
	}
	if low != nil {
		found = append(found, GlanceMetric{Label: "Range", Value: low.str() + "–" + high.str() + "°",
			Detail: fmt.Sprintf("next %d hours", len(periods))})
	}
	for i, period := range periods {
		if periodChance(period) < rainChance {
			continue
		}
		when := periodHour(period)
		if i == 0 {
			when = "Now"
		}
		found = append(found, GlanceMetric{Label: "Rain", Value: fmt.Sprintf("%s · %d%%", when, periodChance(period)),
			Detail: strField(period, "shortForecast")})
		break
	}
	return found
}

// alertMetric is the active alert by name, with how many more there are.
func alertMetric(features []pyValue) []GlanceMetric {
	if len(features) == 0 {
		return []GlanceMetric{}
	}
	first := objectField(&features[0], "properties")
	more := ""
	if len(features) > 1 {
		more = fmt.Sprintf(" +%d", len(features)-1)
	}
	event := strField(first, "event")
	if event == "" {
		event = "Weather alert"
	}
	return []GlanceMetric{{Label: "Alerts", Value: event + more, Detail: strField(first, "headline")}}
}
