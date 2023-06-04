import colorsys
import json
import os
import random
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict

import networkx as nx
import tiktoken
from diskcache import Cache
from pygments.lexers import guess_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound

from aider import prompts

from .dump import dump  # noqa: F402


def to_tree(tags):
    tags = sorted(tags)

    output = ""
    last = [None] * len(tags[0])
    tab = "\t"
    for tag in tags:
        tag = list(tag)

        for i in range(len(last)):
            if last[i] != tag[i]:
                break

        num_common = i
        indent = tab * num_common
        rest = tag[num_common:]
        for item in rest:
            output += indent + item + "\n"
            indent += tab
        last = tag

    return output


def fname_to_components(fname, with_colon):
    path_components = fname.split(os.sep)
    res = [pc + os.sep for pc in path_components[:-1]]
    if with_colon:
        res.append(path_components[-1] + ":")
    else:
        res.append(path_components[-1])
    return res


class RepoMap:
    ctags_cmd = ["ctags", "--fields=+S", "--extras=-F", "--output-format=json"]
    IDENT_CACHE_DIR = ".aider.ident.cache"
    TAGS_CACHE_DIR = ".aider.tags.cache"

    def __init__(self, map_tokens=1024, root=None, main_model="gpt-4", io=None):
        self.io = io

        if not root:
            root = os.getcwd()
        self.root = root

        self.load_ident_cache()
        self.load_tags_cache()

        self.max_map_tokens = map_tokens
        if map_tokens > 0:
            self.has_ctags = self.check_for_ctags()
        else:
            self.has_ctags = False

        self.tokenizer = tiktoken.encoding_for_model(main_model)

    def get_repo_map(self, chat_files, other_files):
        res = self.choose_files_listing(chat_files, other_files)
        if not res:
            return

        files_listing, ctags_msg = res

        if chat_files:
            other = "other "
        else:
            other = ""

        repo_content = prompts.repo_content_prefix.format(
            other=other,
            ctags_msg=ctags_msg,
        )
        repo_content += files_listing

        return repo_content

    def choose_files_listing(self, chat_files, other_files):
        if self.max_map_tokens <= 0:
            return

        if not other_files:
            return

        if self.has_ctags:
            files_listing = self.get_ranked_tags_map(chat_files, other_files)
            num_tokens = self.token_count(files_listing)
            if self.io:
                self.io.tool_output(f"ctags map: {num_tokens/1024:.1f} k-tokens")
            ctags_msg = " with selected ctags info"
            return files_listing, ctags_msg

        files_listing = self.get_simple_files_map(other_files)
        ctags_msg = ""
        num_tokens = self.token_count(files_listing)
        if self.io:
            self.io.tool_output(f"simple map: {num_tokens/1024:.1f} k-tokens")
        if num_tokens < self.max_map_tokens:
            return files_listing, ctags_msg

    def get_simple_files_map(self, other_files):
        fnames = []
        for fname in other_files:
            fname = self.get_rel_fname(fname)
            fname = fname_to_components(fname, False)
            fnames.append(fname)

        return to_tree(fnames)

    def token_count(self, string):
        return len(self.tokenizer.encode(string))

    def get_rel_fname(self, fname):
        return os.path.relpath(fname, self.root)

    def split_path(self, path):
        path = os.path.relpath(path, self.root)
        return [path + ":"]

    def run_ctags(self, filename):
        # Check if the file is in the cache and if the modification time has not changed
        file_mtime = os.path.getmtime(filename)
        cache_key = filename
        if cache_key in self.TAGS_CACHE and self.TAGS_CACHE[cache_key]["mtime"] == file_mtime:
            return self.TAGS_CACHE[cache_key]["data"]

        cmd = self.ctags_cmd + [filename]
        output = subprocess.check_output(cmd).decode("utf-8")
        output = output.splitlines()

        data = [json.loads(line) for line in output]

        # Update the cache
        self.TAGS_CACHE[cache_key] = {"mtime": file_mtime, "data": data}
        self.save_tags_cache()
        return data

    def check_for_ctags(self):
        try:
            with tempfile.TemporaryDirectory() as tempdir:
                hello_py = os.path.join(tempdir, "hello.py")
                with open(hello_py, "w") as f:
                    f.write("def hello():\n    print('Hello, world!')\n")
                self.run_ctags(hello_py)
        except Exception:
            return False
        return True

    def load_tags_cache(self):
        self.TAGS_CACHE = Cache(self.TAGS_CACHE_DIR)

    def save_tags_cache(self):
        pass

    def load_ident_cache(self):
        self.IDENT_CACHE = Cache(self.IDENT_CACHE_DIR)

    def save_ident_cache(self):
        pass

    def get_name_identifiers(self, fname, uniq=True):
        file_mtime = os.path.getmtime(fname)
        cache_key = fname
        if cache_key in self.IDENT_CACHE and self.IDENT_CACHE[cache_key]["mtime"] == file_mtime:
            idents = self.IDENT_CACHE[cache_key]["data"]
        else:
            idents = self.get_name_identifiers_uncached(fname)
            self.IDENT_CACHE[cache_key] = {"mtime": file_mtime, "data": idents}
            self.save_ident_cache()

        if uniq:
            idents = set(idents)
        return idents

    def get_name_identifiers_uncached(self, fname):
        try:
            with open(fname, "r") as f:
                content = f.read()
        except UnicodeDecodeError:
            return list()

        try:
            lexer = guess_lexer_for_filename(fname, content)
        except ClassNotFound:
            return list()

        # lexer.get_tokens_unprocessed() returns (char position in file, token type, token string)
        tokens = list(lexer.get_tokens_unprocessed(content))
        res = [token[2] for token in tokens if token[1] in Token.Name]
        return res

    def get_ranked_tags(self, chat_fnames, other_fnames):
        defines = defaultdict(set)
        references = defaultdict(list)
        definitions = defaultdict(set)

        personalization = dict()

        fnames = set(chat_fnames).union(set(other_fnames))
        chat_rel_fnames = set()

        for fname in sorted(fnames):
            # dump(fname)
            rel_fname = os.path.relpath(fname, self.root)

            if fname in chat_fnames:
                personalization[rel_fname] = 1.0
                chat_rel_fnames.add(rel_fname)

            data = self.run_ctags(fname)

            for tag in data:
                ident = tag["name"]
                defines[ident].add(rel_fname)

                scope = tag.get("scope")
                kind = tag.get("kind")
                name = tag.get("name")
                signature = tag.get("signature")

                last = name
                if signature:
                    last += " " + signature

                res = [rel_fname]
                if scope:
                    res.append(scope)
                res += [kind, last]

                key = (rel_fname, ident)
                definitions[key].add(tuple(res))
                # definitions[key].add((rel_fname,))

            idents = self.get_name_identifiers(fname, uniq=False)
            for ident in idents:
                # dump("ref", fname, ident)
                references[ident].append(rel_fname)

        idents = set(defines.keys()).intersection(set(references.keys()))

        G = nx.MultiDiGraph()

        for ident in idents:
            definers = defines[ident]
            for referencer, num_refs in Counter(references[ident]).items():
                for definer in definers:
                    if referencer == definer:
                        continue
                    G.add_edge(referencer, definer, weight=num_refs, ident=ident)

        if personalization:
            pers_args = dict(personalization=personalization, dangling=personalization)
        else:
            pers_args = dict()

        ranked = nx.pagerank(G, weight="weight", **pers_args)

        # top_rank = sorted([(rank, node) for (node, rank) in ranked.items()], reverse=True)
        # Print the PageRank of each node
        # for rank, node in top_rank:
        #    print(f"{rank:.03f} {node}")

        # distribute the rank from each source node, across all of its out edges
        ranked_definitions = defaultdict(float)
        for src in G.nodes:
            src_rank = ranked[src]
            total_weight = sum(data["weight"] for _src, _dst, data in G.out_edges(src, data=True))
            # dump(src, src_rank, total_weight)
            for _src, dst, data in G.out_edges(src, data=True):
                data["rank"] = src_rank * data["weight"] / total_weight
                ident = data["ident"]
                ranked_definitions[(dst, ident)] += data["rank"]

        ranked_tags = []
        ranked_definitions = sorted(ranked_definitions.items(), reverse=True, key=lambda x: x[1])
        for (fname, ident), rank in ranked_definitions:
            # print(f"{rank:.03f} {fname} {ident}")
            if fname in chat_rel_fnames:
                continue
            ranked_tags += list(definitions.get((fname, ident), []))

        return ranked_tags

    def get_ranked_tags_map(self, chat_fnames, other_fnames=None):
        if not other_fnames:
            other_fnames = list()

        ranked_tags = self.get_ranked_tags(chat_fnames, other_fnames)
        num_tags = len(ranked_tags)

        lower_bound = 0
        upper_bound = num_tags
        best_tree = None

        while lower_bound <= upper_bound:
            middle = (lower_bound + upper_bound) // 2
            tree = to_tree(ranked_tags[:middle])
            num_tokens = self.token_count(tree)
            # dump(middle, num_tokens)

            if num_tokens < self.max_map_tokens:
                best_tree = tree
                lower_bound = middle + 1
            else:
                upper_bound = middle - 1

        return best_tree


def find_py_files(directory):
    if not os.path.isdir(directory):
        return [directory]

    py_files = []
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".py"):
                py_files.append(os.path.join(root, file))
    return py_files


def get_random_color():
    hue = random.random()
    r, g, b = [int(x * 255) for x in colorsys.hsv_to_rgb(hue, 1, 0.75)]
    res = f"#{r:02x}{g:02x}{b:02x}"
    return res


if __name__ == "__main__":
    fnames = sys.argv[1:]

    fnames = []
    for dname in sys.argv[1:]:
        fnames += find_py_files(dname)

    fnames = sorted(fnames)

    root = os.path.commonpath(fnames)

    rm = RepoMap(root=root)
    repo_map = rm.get_ranked_tags_map(fnames)

    dump(len(repo_map))
    print(repo_map)