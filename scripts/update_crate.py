import os
from pathlib import Path
from rocrate.rocrate import ROCrate
from rocrate.model.person import Person
from rocrate.model.data_entity import DataEntity
from rocrate.model.contextentity import ContextEntity
from git import Repo
from git.exc import InvalidGitRepositoryError, GitCommandError
import json
import argparse
import datetime
import nbformat
import sys
import requests
from bs4 import BeautifulSoup
from github import Github
import re
import arrow
import git

LICENCES = json.loads(Path("scripts", "licences.json").read_text())
CONTEXT_PROPERTIES = [
    "author",
    "action",
    "workExample",
    "mainEntityOfPage",
    "subjectOf",
    "isBasedOn",
    "distribution",
    "isPartOf",
    "license"
]


def main(crate_path, defaults, version, data_repo):
    # Make working directory the parent of the scripts directory
    os.chdir(Path(__file__).resolve().parent.parent)
    crate_maker = CrateMaker(crate_path, defaults=defaults, version=version, data_repo=data_repo)
    # Update the crate
    crate_maker.update_crate()


def listify(value):
    if not isinstance(value, list):
        return [value]
    return value


def delistify(value):
    if isinstance(value, list) and (len(value) == 1 or len(set(value)) == 1):
        return value[0]
    else:
        return value


class CrateMaker:

    def __init__(self, crate_path="./", defaults=None, version=None, data_repo=None):
        # Make working directory the parent of the scripts directory
        os.chdir(Path(__file__).resolve().parent.parent)
        self.defaults = defaults
        self.crate_path = crate_path
        self.version = version
        self.data_repo = data_repo

    def id_ify(self, elements):
        """Wraps elements in a list with @id keys
        eg, convert ['a', 'b'] to [{'@id': 'a'}, {'@id': 'b'}]
        """
        # If the input is a string, make it a list
        # elements = [elements] if isinstance(elements, str) else elements
        # Nope - single elements shouldn't be lists, see: https://www.researchobject.org/ro-crate/1.1/appendix/jsonld.html
        if isinstance(elements, str):
            return {"@id": elements}
        elif isinstance(elements, list):
            try:
                return [{"@id": e.id} for e in elements]
            except AttributeError:
                return [{"@id": element} for element in elements]

    def creates_data(self, notebook):
        """
        Check to see if a notebook creates a file in the specified data repo.
        """
        if not self.data_repo:
            return True
        else:
            metadata = self.get_nb_metadata(notebook)
            owner, repo = self.get_gh_parts(self.data_repo)
            for action in metadata.get("action", []):
                for result in action.get("result", []):
                    if f"{owner}/{repo}" in result.get("url", ""):
                        return True
        return False

    def get_notebooks(self, path="."):
        """Returns a list of paths to jupyter notebooks in the given directory

        Parameters:
            dir: The path to the directory in which to search.

        Returns:
            Paths of the notebooks found in the directory
        """
        files = Path(path).glob("*.ipynb")
        is_notebook = lambda file: not file.name.lower().startswith(
            ("draft", "untitled", "index")
        ) and self.creates_data(file)
        return list(filter(is_notebook, files))

    def update_properties(self, entry, updates, exclude=[]):
        for key, value in updates.items():
            if key in CONTEXT_PROPERTIES:
                self.add_entities(entry, key, listify(value))
            elif not key.startswith("@") and key not in exclude:
                entry[key] = value
        return entry

    def add_people(self, authors):
        """Converts a list of authors to a list of Persons to be embedded within an ROCrate

        Parameters:
            crate: The rocrate in which the authors will be created.
            authors:
                A list of author information.
                Expects a dict with at least a 'name' value ('Surname, Givenname')
                If there's an 'orcid' this will be used as the id (and converted to a uri if necessary)
        Returns:
            A list of Persons.
        """
        added = []
        # Loop through list of authors
        for author_data in authors:
            # If there's no orcid, create an id from the name
            if not author_data.get("orcid"):
                author_id = f"#{author_data['name'].replace(', ', '_')}"

            # If there's an orcid but it's not a url, turn it into one
            elif not author_data["orcid"].startswith("http"):
                author_id = f"https://orcid.org/{author_data['orcid']}"

            # Otherwise we'll just use the orcid as the id
            else:
                author_id = author_data["orcid"]
            # Check to see if there's already an entry for this person in the crate
            author = self.crate.get(author_id)

            # If there's already an entry we'll update the existing properties
            if not author:
                props = {"name": author_data["name"]}
                author = self.crate.add(Person(self.crate, author_id, properties=props))
            added.append(self.update_properties(author, author_data, exclude=["orcid"]))
        return added

    def add_update_action(self, version):
        """
        Adds an UpdateAction to the crate when the repo version is updated.
        """
        # Create an id for the action using the version number
        action_id = f"create_version_{version.replace('.', '_')}"

        # Set basic properties for action
        properties = {
            "@type": "UpdateAction",
            "endDate": datetime.datetime.now().strftime("%Y-%m-%d"),
            "name": f"Create version {version}",
            "actionStatus": {"@id": "http://schema.org/CompletedActionStatus"},
        }

        # Create entity
        self.crate.add(ContextEntity(self.crate, action_id, properties=properties))

    def add_context_entity(self, entity):
        """
        Adds a ContextEntity to the crate.

        Parameters:
            crate: the current ROCrate
            entity: A JSONLD ready dict containing "@id" and "@type" values
        """
        return self.crate.add(
            ContextEntity(self.crate, entity["@id"], properties=entity)
        )

    def add_page(self, page_data, type="CreativeWork"):
        """
        Create a context entity for a HTML page or resource
        """
        # If it's a url string, convert to a dict
        if isinstance(page_data, str):
            page_data = {"url": page_data}
        page_id = page_data["url"]
        # Check if there's already an entity for this page
        page = self.crate.get(page_id)
        # If there's not an existing page entity, create one
        if not page:
            # Default properties, might be overwritten by values from page_data
            props = {
                "@id": page_id,
                "@type": type,
                "url": page_data["url"],
            }
            # Create a new context entity for the page
            page = self.add_context_entity(props)
        # Update the context entity with additional properties from page data
        page = self.update_properties(page, page_data)
        # If there's a specific name in an existing record we want to keep it.
        # Otherwise add a default name from the page title
        default_name = self.get_page_title(page_data["url"])
        if "name" in page_data and page.get("name") == default_name:
            page["name"] = page_data["name"]
        elif not page.get("name"):
            page["name"] = default_name
        return page

    def add_pages(self, pages, type="CreativeWork"):
        """
        Add related pages
        """
        added = []
        for page in pages:
            if page:
                added.append(self.add_page(page, type=type))
        return added

    def add_licence(self, licences):
        added = []
        for licence in licences:
            added.append(self.add_context_entity(LICENCES[licence]))
        return added

    def add_download(self, downloads):
        added = []
        for url in downloads:
            download = {
                "@id": url,
                "@type": "DataDownload",
                "name": "Download repository as zip",
                "url": url
            }
            added.append(self.add_context_entity(download))
        return added

    def add_entities(self, record, entity_type, entities):
        if entity_type == "author":
            added = self.add_people(entities)
        elif entity_type == "action":
            added = self.add_actions(record, entities)
        elif entity_type == "license":
            added = self.add_licence(entities)
        elif entity_type == "isBasedOn":
            added = self.add_pages(entities, type="SoftwareSourceCode")
        elif entity_type == "distribution":
            added = self.add_download(entities)
        else:
            added = self.add_pages(entities)
        if added and entity_type != "action":
            record[entity_type] = delistify(added)

    def get_local_file_stats(self, local_path):
        stats = {}
        local_file = Path(local_path)
        if local_file.is_dir():
            stats["size"] = len(list(local_file.glob("*")))
            file_stats = local_file.stat()
            stats["dateModified"] = arrow.get(file_stats.st_mtime).isoformat()
        else:
            stats["sdDatePublished"] = arrow.utcnow().isoformat()
            # Get file stats from local filesystem
            file_stats = local_file.stat()
            stats["contentSize"] = file_stats.st_size
            stats["dateModified"] = arrow.get(file_stats.st_mtime).isoformat()
            if local_file.name.endswith((".csv", ".ndjson")):
                stats["size"] = 0
                with local_file.open("r") as df:
                    for line in df:
                        stats["size"] += 1
        return stats

    def get_web_file_stats(self, url):
        stats = {"sdDatePublished": arrow.utcnow().isoformat()}
        if "github" in url:
            repo = self.get_gh_repo(url)
            file_path = url.split(f"/{repo.default_branch}/")[-1]
            contents = repo.get_contents(file_path)
            stats["contentSize"] = contents.size
            stats["dateModified"] = contents.last_modified_datetime.isoformat()
        else:
            response = requests.head(url)
            stats["contentSize"] = response.headers.get("Content-length")
            stats["dateModified"] = arrow.get(
                response.headers.get("Last-Modified"), "ddd, D MMM YYYY HH:mm:ss ZZZ"
            ).isoformat()
        return stats

    def get_gh_parts(self, url):
        try:
            owner, repo = re.search(
                r"https*://.*(?:github|githubusercontent).com/(.+?)/([a-zA-Z0-9\-_]+)", url
            ).groups()
        except AttributeError:
            owner = None
            repo = None
        return owner, repo

    def get_gh_repo(self, url):
        owner, repo = self.get_gh_parts(url)
        if owner and repo:
            g = Github()
            return g.get_repo(full_name_or_id=f"{owner}/{repo}")

    def get_gh_path(self, url):
        default_branch = self.get_default_gh_branch(url)
        return url.split(f"/{default_branch}/")[-1]

    def get_repo_info(self):
        # Try to get some info from the local git repo
        try:
            repo = git.Repo(".")
            repo_url = repo.remotes.origin.url.replace(
                ".git", "/"
            )
            repo_name = repo_url.strip("/").split("/")[-1]
        # There is no git repo or no remote set
        except (InvalidGitRepositoryError, GitCommandError):
            repo_url = ""
            repo_name = "example-repo"
        return repo_name, repo_url

    def get_repo_link(self, entry):
        """
        Files and notebooks are usually part of a code repository.
        Also crate can have a codeRepository prop.
        If the files have urls then use the url to get repo.
        Otherwise use the local git info to get repo url.
        """
        repo_url = None
        if url := entry.get("url"):
            owner, repo = self.get_gh_parts(url)
            if owner and repo:
                repo_url = f"https://github.com/{owner}/{repo}"
        else:
            _, repo_url = self.get_repo_info()
        return repo_url

    def add_repo_link(self, entry):
        if "isPartOf" not in entry:
            repo_link = self.get_repo_link(entry)
            if repo_link:
                entry["isPartOf"] = repo_link
        return entry

    def get_default_gh_branch(self, url):
        """
        Get the default branch of a GH repository from a url that points to it.
        """
        repo = self.get_gh_repo(url)
        return repo.default_branch

    def get_gh_file_url(self, file_path):
        """
        Construct a url to that points to a notebook file in the code repository.
        Note that you could get the url from the GH repo, but there's a possibility
        that this script will be run before every notebook has been committed and pushed.
        """
        _, repo_url = self.get_repo_info()
        default_branch = self.get_default_gh_branch(repo_url)
        return f"{repo_url.strip('/')}/blob/{default_branch}/{file_path}"


    def add_files(self, files):
        added = []
        for data_file in files:
            local_path = data_file.get("localPath")
            url = data_file.get("url")
            if url or local_path:
                props = {"@type": ["File", "Dataset"]}
                data_file = self.add_repo_link(data_file)
                if url:
                    props["name"] = data_file.get("name", os.path.basename(url))
                    if local_path:
                        props.update(self.get_local_file_stats(local_path))
                    else:
                        props.update(self.get_web_file_stats(url))
                    fetch_remote = False
                    file_id = url
                    if self.data_repo:
                        # We want the file ids to be relative to the data crate, so use the local path
                        # or fetch_remote to put them in the right place.
                        if local_path:
                            file_id = local_path
                        else:
                            fetch_remote = True
                    file_added = self.crate.add_file(
                        file_id, properties=props, fetch_remote=fetch_remote
                    )
                elif local_path:
                    props["name"] = data_file.get("name", os.path.basename(local_path))
                    props.update(self.get_local_file_stats(local_path))
                    file_added = self.crate.add_file(
                        local_path, properties=props, dest_path=local_path
                    )
                file_added = self.update_properties(
                    file_added, data_file, exclude=["localPath"]
                )
                added.append(file_added)
        return added
    
    def file_in_repo(self, data_file):
        """
        Check a data file's url to see if it's part of the data repo specified
        by the --data-repo parameter.
        """
        owner, repo = self.get_gh_parts(self.data_repo)
        if f"{owner}/{repo}" in data_file.get("url", ""):
            return True

    def filter_files(self, action_data, file_relation):
        files = listify(action_data.get(file_relation, []))
        if self.data_repo:
            return list(filter(self.file_in_repo, files))
        else:
            return files

    def add_actions(self, notebook, actions):
        added = []
        for index, action_data in enumerate(actions):
            action_id = (
                f"#{os.path.basename(notebook.id).replace('.ipynb', '')}_run_{index}"
            )
            props = {
                "@id": action_id,
                "@type": "CreateAction",
                "instrument": self.id_ify(notebook.id),
                "actionStatus": {"@id": "http://schema.org/CompletedActionStatus"},
                "name": f"Run of notebook: {os.path.basename(notebook.id)}",
            }
            file_dates = []
            for file_relation in ["result", "object"]:
                added_files = self.add_files(
                    self.filter_files(action_data, file_relation)
                )
                if added_files:
                    props[file_relation] = delistify(added_files)
                    for data_file in added_files:
                        file_dates.append(data_file.get("dateModified"))
            props["endDate"] = sorted(file_dates)[-1]
            action = self.add_context_entity(props)
            action = self.update_properties(
                action, action_data, exclude=["result", "object"]
            )
            self.crate.root_dataset.append_to("mentions", action)
            added.append(action)
        return added

    def add_python_version(self):
        # I could also get this from notebook metadata
        # Get the version components from the system
        major, minor, micro = sys.version_info[0:3]
        # Construct url and version name
        url = f"https://www.python.org/downloads/release/python-{major}{minor}{micro}/"
        version = f"{major}.{minor}.{micro}"
        # Define properties of context entity
        entity = {
            "@id": url,
            "version": version,
            "name": f"Python {version}",
            "url": url,
            "@type": ["ComputerLanguage", "SoftwareApplication"],
        }
        return self.crate.add(
            ContextEntity(self.crate, entity["@id"], properties=entity)
        )

    def get_page_title(self, url):
        """
        Get title of the page at the supplied url.
        """
        response = requests.get(url)
        if response.ok:
            soup = BeautifulSoup(response.text, features="lxml")
            return soup.title.string.strip()

    def get_nb_metadata(self, notebook):
        nb = nbformat.read(notebook, nbformat.NO_CONVERT)
        return {k: v for k, v in nb.metadata.rocrate.items() if v}

    def add_notebook(self, notebook):
        gh_url = self.get_gh_file_url(notebook)
        if self.data_repo:
            nb_id = gh_url
        else:
            nb_id = notebook
        # Get metadata embedded in notebooks
        nb_metadata = self.get_nb_metadata(notebook)
        nb_metadata = self.add_repo_link(nb_metadata)
        # Default notebook properties
        nb_props = {
            "@type": ["File", "SoftwareSourceCode"],
            "encodingFormat": "application/x-ipynb+json",
            "programmingLanguage": self.id_ify(self.add_python_version().id),
            "conformsTo": self.id_ify(
                "https://purl.archive.org/textcommons/profile#Notebook"
            ),
            "url": gh_url,
        }
        # Add notebook to crate
        new_nb = self.crate.add_file(nb_id, properties=nb_props)
        # Add properties from notebook metadata
        new_nb = self.update_properties(new_nb, nb_metadata)
        return new_nb

    def get_old_crate_data(self, crate_source="./"):
        try:
            old_crate = ROCrate(source=crate_source)
            old_root = old_crate.get("./")
            # Get the old root properties
            old_props = old_root.properties()
            # Add old properties to new record (except for those that will be populated from notebooks)
            root_props = {
                k: v
                for k, v in old_props.items()
                if k in ["name", "description", "mainEntityOfPage"] and not isinstance(v, dict)
            }
            entities = {k: old_crate.get(v["@id"]) for k, v in old_props.items()
                if k in ["mainEntityOfPage", "license"] and isinstance(v, dict)}
            # Get version UpdateAction records for inclusion in new crate
            versions = old_crate.get_by_type("UpdateAction")
        # If there's not an existing crate, try to set some default properties
        except (ValueError, FileNotFoundError):
            root_props = {}
            versions = []
            entities = {}
        return root_props, entities, versions

    def prepare_data_crate(self):
        _, repo_name = self.get_gh_parts(self.data_repo)
        if repo_name:
            crate_source = f"./{repo_name}-rocrate"
        else:
            crate_source = "./data-rocrate"
        _, code_repo_url = self.get_repo_info()
        root_props, entities, versions = self.get_old_crate_data(crate_source)
        if not root_props:
            root_props = {
                "name": self.defaults.get("name", repo_name),
                "description": self.defaults.get("description", ""),
                "isBasedOn": self.defaults.get("isBasedOn", code_repo_url),
                "distribution": f"{self.data_repo.rstrip('/')}/archive/refs/heads/main.zip",
            }
            versions = []
            entities = {}
        return root_props, crate_source, entities, versions

    def prepare_code_crate(self):
        # Load data from an existing crate
        root_props, entities, versions = self.get_old_crate_data()
        if not root_props:
            # Get info from git
            repo_name, repo_url = self.get_repo_info()
            root_props = {
                "name": self.defaults.get("name", repo_name),
                "description": self.defaults.get("description", ""),
                "codeRepository": self.defaults.get("codeRepository", repo_url),
            }
            versions = []
        return root_props, "./", entities, versions

    def update_crate(self):
        if self.data_repo:
            root_props, crate_source, entities, versions = self.prepare_data_crate()
        else:
            root_props, crate_source, entities, versions = self.prepare_code_crate()
        self.crate = ROCrate()
        # Add properties to the root
        root = self.crate.get("./")
        # update_jsonld doesn't seem to work here?
        #for p, v in root_props.items():
        #    root[p] = v
        root = self.update_properties(root, root_props)
        # Add version information
        for v in versions:
            self.crate.add(v)
        for k, v in entities.items():
            root[k] = self.id_ify(v.id)
            self.crate.add(v)
        # Add authors from defaults
        self.add_entities(root, "author", self.defaults.get("authors", []))
        # If this is a new version, change version number and add UpdateAction
        if self.version:
            root["version"] = self.version
            self.add_update_action(self.version)
        # Add notebooks
        for notebook in self.get_notebooks():
            nb = self.add_notebook(notebook)
            for author in listify(nb.get("author")):
                if author not in root.get("author", []):
                    root.append_to("author", author)
        # Set licence of crate metadata
        root["license"] = self.add_context_entity(LICENCES["metadata"])
        # Save crate
        self.crate.write(crate_source)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--defaults",
        type=str,
        help="File containing Crate default values",
        required=False,
    )
    parser.add_argument(
        "--crate-path", type=str, help="Path to crate", default="./"
    )
    parser.add_argument(
        "--version", type=str, help="New version number", required=False
    )
    parser.add_argument("--data-repo", type=str, default="", required=False)
    args = parser.parse_args()
    if args.defaults:
        defaults = json.loads(Path(args.defaults).read_text())
    else:
        defaults = {}

    main(defaults=defaults, crate_path=args.crate_path, version=args.version, data_repo=args.data_repo)
