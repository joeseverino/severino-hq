package providers

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// githubContent is GitHub's answer for a file's contents.
func githubContent(text string) string {
	encoded, _ := json.Marshal(map[string]string{"type": "file", "encoding": "base64", "content": base64.StdEncoding.EncodeToString([]byte(text))})
	return string(encoded)
}

// A repository opted in to updates names each app's Compose files on its
// default branch, and each update's pull request the image every service
// moves to, as Compose loads the app's files: the override after the main
// file, a variable's default, a profile's service, and a variable no image
// needs left unread.
func TestAnOptedInRepositoryReadsItsApps(t *testing.T) {
	api := func(path string) string { return githubFakeAPI + " /repos/example/app" + path }
	routes := githubRepositoryRoutes()
	routes[api("/git/trees/main")] = `{"sha":"t0","truncated":false,"tree":[{"path":"compose","mode":"040000","type":"tree","sha":"t1"},{"path":"README.md","mode":"100644","type":"blob","sha":"b0"}]}`
	routes[api("/git/trees/t1?recursive=1")] = `{"sha":"t1","truncated":false,"tree":[
		{"path":"example","mode":"040000","type":"tree","sha":"t2"},
		{"path":"example/compose.yml","mode":"100644","type":"blob","sha":"b1"},
		{"path":"example/docker-compose.override.yml","mode":"100644","type":"blob","sha":"b2"},
		{"path":"example/nginx.conf","mode":"100644","type":"blob","sha":"b3"},
		{"path":"web/compose.yaml","mode":"100644","type":"blob","sha":"b4"},
		{"path":"web/deep/compose.yml","mode":"100644","type":"blob","sha":"b5"}]}`
	head := "/contents/compose/%s?ref=aaaa000000000000000000000000000000000000"
	routes[api(fmt.Sprintf(head, "example/compose.yml"))] = githubContent("services:\n  web:\n    image: example/app:1\n  worker:\n    image: example/worker:${TAG:-3}@sha256:" + strings.Repeat("a", 64) + "\n  viewer:\n    build: ./viewer\n    image: example-viewer:local\n  reader:\n    build:\n      context: ./reader\n")
	routes[api(fmt.Sprintf(head, "example/docker-compose.override.yml"))] = githubContent("services:\n  worker:\n    environment:\n      LEVEL: info\n")
	commit := func(sha string) string {
		return `{"sha":"` + sha + `","html_url":"https://github.com/example/app/commit/` + sha + `","commit":{"message":"Change"},"author":null}`
	}
	routes[api("/commits?sha=aaaa000000000000000000000000000000000000&per_page=100")] = "[" + commit("aaaa000000000000000000000000000000000000") + "," + commit("c2") + "," + commit("c1") + "," + commit("c0") + "]"
	routes[api("/commits?sha=aaaa000000000000000000000000000000000000&path=compose%2Fexample&per_page=1")] = "[" + commit("c1") + "]"
	routes[api(fmt.Sprintf(head, "web/compose.yaml"))] = githubContent("services:\n  web:\n    image: example/web:${WEB_TAG}\n")
	routes[api("/contents/compose/example/compose.yml?ref=dddd")] = githubContent("services:\n  web:\n    image: example/app:1\n  worker:\n    image: example/worker:${TAG:-3}\n")
	routes[api("/contents/compose/example/compose.yml?ref=eeee")] = githubContent("services:\n  web:\n    image: example/app:2\n    environment:\n      SECRET: ${SECRET}\n  worker:\n    image: example/worker:${TAG:-3}\n  cache:\n    image: example/cache:7\n    profiles: [cache]\n")
	routes[api("/contents/compose/example/docker-compose.override.yml?ref=dddd")] = githubContent("services:\n  worker:\n    environment:\n      LEVEL: info\n")
	routes[api("/contents/compose/example/docker-compose.override.yml?ref=eeee")] = githubContent("services:\n  worker:\n    image: example/worker:4\n")
	h := newGitHubHarness(t, routes, runtime.Environment{})
	for _, ref := range []string{"dddd", "eeee"} {
		h.refuse(githubFakeAPI, "/repos/example/app/contents/compose/large/compose.yml?ref="+ref, http.StatusNotFound)
	}
	NewController(h.r, runtime.ControllerRegistry{DependencyUpdates: runtime.DependencyUpdatesPlan{
		Owners: []runtime.DependencyUpdatesOwner{{Owner: "example", Repositories: []string{"example/app"}}},
	}})
	record, _, pulls, refused := readPass(t, h)
	if len(refused) != 0 {
		t.Fatalf("refused %+v", refused)
	}
	encoded, _ := json.Marshal(record)
	var view map[string]any
	_ = json.Unmarshal(encoded, &view)
	for _, test := range []struct {
		name string
		got  any
		want string
	}{
		{"each app's Compose files, by the names Compose finds in a directory", record.Compose, `["compose/example/compose.yml","compose/example/docker-compose.override.yml","compose/web/compose.yaml"]`},
		{"the login the update run opens them as, the App's bot", view["updater"], `"hq-example[bot]"`},
		{"each service's image at the head, and whether it builds it, an app that needs the host's variables left out", record.ComposeImages, `[{"path":"compose/example/compose.yml","service":"reader","image":"","build":true},{"path":"compose/example/compose.yml","service":"viewer","image":"example-viewer:local","build":true},{"path":"compose/example/compose.yml","service":"web","image":"example/app:1","build":false},{"path":"compose/example/compose.yml","service":"worker","image":"example/worker:3@sha256:` + strings.Repeat("a", 64) + `","build":false}]`},
		{"each app built on its host, and the commits from the last that changed its directory to the head", record.Builds, `[{"app":"example","commits":["aaaa000000000000000000000000000000000000","c2","c1"]}]`},
		{"each service the update's head moves", pulls[0].Apps, `[{"app":"example","read":true,"images":[{"service":"cache","before":"","after":"example/cache:7"},{"service":"web","before":"example/app:1","after":"example/app:2"},{"service":"worker","before":"example/worker:3","after":"example/worker:4"}]},{"app":"large","read":true,"images":[]}]`},
		{"no images for a pull request that is not an update", pulls[1].Apps, `null`},
	} {
		t.Run(test.name, func(t *testing.T) {
			if encoded, _ := json.Marshal(test.got); string(encoded) != test.want {
				t.Fatalf("got  %s\nwant %s", encoded, test.want)
			}
		})
	}
}

// A file an update adds is absent at its base, and an app whose files do not
// load, or whose image needs a variable only the host sets, is read false,
// with no images: the reading never guesses a move. An app whose build inputs
// alone change, at any depth of its directory, is a change that moves no
// image, and a file beside the apps is none.
func TestAnAppThatDoesNotLoadMovesNothing(t *testing.T) {
	api := func(path string) string { return githubFakeAPI + " /repos/example/app" + path }
	routes := githubRepositoryRoutes()
	routes[api("/git/trees/main")] = `{"sha":"t0","truncated":false,"tree":[]}`
	routes[api("/contents/compose/example/compose.yml?ref=eeee")] = githubContent("services:\n  web:\n    image: example/app:2\n")
	routes[api("/contents/compose/large/compose.yml?ref=eeee")] = githubContent("services: [not, a, mapping]\n")
	routes[api("/contents/compose/large/compose.yml?ref=dddd")] = githubContent("services: {}\n")
	routes[api("/pulls/6/files?per_page=100")] = `[{"filename":"compose/example/compose.yml","status":"added","additions":3,"deletions":0},
		{"filename":"compose/large/compose.yml","status":"modified","additions":1,"deletions":1},
		{"filename":"compose/unset/compose.yml","status":"modified","additions":1,"deletions":1},
		{"filename":"compose/built/Dockerfile","status":"modified","additions":1,"deletions":1},
		{"filename":"compose/built/reader/go.mod","status":"modified","additions":1,"deletions":1},
		{"filename":"compose/notes.md","status":"modified","additions":1,"deletions":1}]`
	routes[api("/contents/compose/unset/compose.yml?ref=dddd")] = githubContent("services:\n  db:\n    image: example/db:1\n")
	routes[api("/contents/compose/unset/compose.yml?ref=eeee")] = githubContent("services:\n  db:\n    image: example/db:${DB_TAG}\n")
	h := newGitHubHarness(t, routes, runtime.Environment{})
	h.refuse(githubFakeAPI, "/repos/example/app/contents/compose/example/compose.yml?ref=dddd", http.StatusNotFound)
	NewController(h.r, runtime.ControllerRegistry{DependencyUpdates: runtime.DependencyUpdatesPlan{
		Owners: []runtime.DependencyUpdatesOwner{{Owner: "example", Repositories: []string{"Example/App"}}},
	}})
	record, _, pulls, _ := readPass(t, h)
	want := `[{"app":"built","read":true,"images":[]},{"app":"example","read":true,"images":[{"service":"web","before":"","after":"example/app:2"}]},{"app":"large","read":false,"images":[]},{"app":"unset","read":false,"images":[]}]`
	if encoded, _ := json.Marshal(pulls[0].Apps); string(encoded) != want || record.Compose == nil || len(record.Compose) != 0 {
		t.Fatalf("apps %s, compose %v", encoded, record.Compose)
	}
}
