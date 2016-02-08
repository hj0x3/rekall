# Rekall Memory Forensics
# Copyright 2015 Google Inc. All Rights Reserved.
#
# Author: Michael Cohen scudette@google.com
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#

"""
This plugin manages the profile repository.

"""
import json
import os
import yaml

from rekall import io_manager
from rekall import obj
from rekall import plugin
from rekall import registry
from rekall import testlib
from rekall import utils


class RepositoryManager(io_manager.DirectoryIOManager):
    """We manage the repository using YAML.

    YAML is more user friendly than JSON.
    """

    def Encoder(self, data, **_):
        return utils.PPrint(data)

    def Decoder(self, raw):
        try:
            # First try to load it with json because it is way faster.
            return super(RepositoryManager, self).Decoder(raw)
        except ValueError:
            # If that does not work, try to load it with yaml.
            return yaml.safe_load(raw)


class RepositoryPlugin(object):
    """A plugin to manage a type of profile in the repository."""
    __metaclass__ = registry.MetaclassRegistry

    def __init__(self, session=None, **kwargs):
        """Instantiate the plugin with the provided kwargs."""
        self.args = utils.AttributeDict(kwargs)
        self.session = session

    def TransformProfile(self, profile):
        """Transform the profile according to the specified transforms."""
        transforms = self.args.transforms or {}
        for transform, args in transforms.items():
            if transform == "merge":
                profile["$MERGE"] = args
            else:
                raise RuntimeError("Unknown transform %s" % transform)

        return profile

    def BuildIndex(self):
        repository = self.args.repository
        spec = repository.GetData(self.args.index)

        index = self.session.plugins.build_index(
            manager=repository).build_index(spec)

        repository.StoreData("%s/index" % self.args.profile_name, index)

    def Build(self, renderer):
        """Implementation of the build routine."""


class WindowsGUIDProfile(RepositoryPlugin):
    """Manage a Windows profile from the symbol server."""

    def FetchPDB(self, temp_dir, guid, pdb_filename):
        self._RunPlugin("fetch_pdb", pdb_filename=pdb_filename,
                        guid=guid, dump_dir=temp_dir)

        data = open(os.path.join(temp_dir, pdb_filename)).read()
        repository = self.args.repository

        repository.StoreData("src/pdb/%s.pdb" % guid, data, raw=True)

    def ParsePDB(self, temp_dir, guid, original_pdb_filename):
        repository = self.args.repository
        data = repository.GetData("src/pdb/%s.pdb" % guid, raw=True)
        pdb_filename = os.path.join(temp_dir, guid + ".pdb")
        output_filename = os.path.join(temp_dir, guid)
        with open(pdb_filename, "wb") as fd:
            fd.write(data)

        profile_class = (self.args.profile_class or
                         original_pdb_filename.capitalize())
        self._RunPlugin(
            "parse_pdb", pdb_filename=pdb_filename, profile_class=profile_class,
            output=output_filename)

        profile_data = json.loads(open(output_filename, "rb").read())
        profile_data = self.TransformProfile(profile_data)
        repository.StoreData("%s/%s" % (self.args.profile_name, guid),
                             profile_data)

    def Build(self, renderer):
        repository = self.args.repository
        guid_file = self.args.repository.GetData(self.args.guids)

        changed_files = False
        for pdb_filename, guids in guid_file.iteritems():
            for guid in guids:
                # If the profile exists in the repository continue.
                if repository.Metadata(
                        "%s/%s" % (self.args.profile_name, guid)):
                    continue

                renderer.format("Building profile {0}/{1}\n",
                                self.args.profile_name, guid)

                # Otherwise build it.
                changed_files = True

                with utils.TempDirectory() as temp_dir:
                    # Do we need to fetch the pdb file?
                    if not repository.Metadata("src/pdb/%s.pdb" % guid):
                        self.FetchPDB(temp_dir, guid, pdb_filename)

                    self.ParsePDB(temp_dir, guid, pdb_filename)

        if changed_files and self.args.index or self.args.force_build_index:
            renderer.format("Building index for profile {0} from {1}\n",
                            self.args.profile_name, self.args.index)

            self.BuildIndex()


class CopyAndTransform(RepositoryPlugin):
    """A profile processor which copies and transforms."""

    def Build(self, renderer):
        repository = self.args.repository
        profile_metadata = repository.Metadata(self.args.profile_name)
        source_metadata = repository.Metadata(self.args.source)
        if not profile_metadata or (
                source_metadata["LastModified"] >
                profile_metadata["LastModified"]):
            data = repository.GetData(self.args.source)

            # Transform the data as required.
            data = self.TransformProfile(data)
            repository.StoreData(self.args.profile_name, utils.PPrint(data),
                                 raw=True)
            renderer.format("Building profile {0} from {1}\n",
                            self.args.profile_name, self.args.source)


class OSXProfile(RepositoryPlugin):
    """Build OSX Profiles."""

    def Build(self, renderer):
        repository = self.args.repository
        changed_files = False
        for source in self.args.sources:
            profile_name = "OSX/%s" % source.split("/")[-1]
            profile_metadata = repository.Metadata(profile_name)

            # Profile does not exist - rebuild it.
            if not profile_metadata:
                data = repository.GetData(source)

                # Transform the data as required.
                data = self.TransformProfile(data)
                repository.StoreData(profile_name, utils.PPrint(data),
                                     raw=True)
                renderer.format("Building profile {0} from {1}\n",
                                profile_name, source)
                changed_files = True

        if changed_files and self.args.index:
            renderer.format("Building index for profile {0} from {1}\n",
                            self.args.profile_name, self.args.index)

            self.BuildIndex()


class LinuxProfile(RepositoryPlugin):
    """Build Linux profiles."""

    def Build(self, renderer):
        """Linux profile location"""
        convert_profile = self.session.plugins.convert_profile(
            session=self.session,
            out_file="dummy file")  # We don't really output the profile.

        # Open the previous index to update it.
        index = self.session.LoadProfile("Linux/index")

        changed_files = False
        total_profiles = 0
        new_profiles = 0

        for source_profile in self.args.repository.ListFiles():
            # Find all source profiles.
            if (source_profile.startswith("src/Linux") and
                source_profile.endswith(".zip")):

                total_profiles += 1
                profile_id = source_profile.lstrip("src/").rstrip(".zip")

                # Skip already built profiles.
                if self.args.repository.Metadata(profile_id):
                    continue

                # Convert the profile
                self.session.report_progress(
                    "Found new raw Linux profile %s. Converting...", profile_id)
                self.session.logging.info(
                    "Found new raw Linux profile %s", profile_id)

                profile_fullpath = self.args.repository.GetAbsolutePathName(
                    source_profile)
                profile = convert_profile.ConvertProfile(
                    io_manager.Factory(
                        profile_fullpath, session=self.session, mode="r"))
                if not profile:
                    self.session.logging.info(
                        "Skipped %s, Unable to convert to a Rekall profile.",
                        profile_path)
                    continue

                # Add profile to the repository and the inventory
                self.args.repository.StoreData(profile_id, profile)
                new_profiles += 1
                changed_files = True

        self.session.logging.info("Found %d profiles. %d are new.",
                                  total_profiles, new_profiles)
        # Now rebuild the index
        if changed_files and self.args.index or self.args.force_build_index:
            self.BuildIndex()


class ManageRepository(plugin.Command):
    """Manages the profile repository."""

    name = "manage_repo"

    @classmethod
    def args(cls, parser):
        super(ManageRepository, cls).args(parser)

        parser.add_argument(
            "path_to_repository", default=".",
            help="The path to the profile repository")
        parser.add_argument(
            "--build_targets", type="ArrayStringParser",
            help="A list of targets to build.")
        parser.add_argument(
            "--force_build_index", type="Boolean", default=False,
            help="Forces building the index.")


    def __init__(self, command=None, path_to_repository=None,
                 build_targets=None, force_build_index=False, **kwargs):
        super(ManageRepository, self).__init__(**kwargs)
        self.command = command
        self.path_to_repository = os.path.abspath(path_to_repository)
        self.build_targets = build_targets
        self.force_build_index = force_build_index

        # Check if we can load the repository config file.
        self.repository = RepositoryManager(
            self.path_to_repository, session=self.session)

        self.config_file = self.repository.GetData("config.yaml")

    def render(self, renderer):
        for profile_name, kwargs in self.config_file.iteritems():
            if self.build_targets and profile_name not in self.build_targets:
              continue

            handler_type = kwargs.pop("type", None)
            if not handler_type:
                raise RuntimeError(
                    "Unspecified repository handler for profile %s" %
                    profile_name)

            handler_cls = RepositoryPlugin.classes.get(handler_type)
            if handler_cls is None:
                raise RuntimeError(
                    "Unknown repository handler %s" % handler_type)

            handler = handler_cls(
                session=self.session, repository=self.repository,
                profile_name=profile_name,
                force_build_index=self.force_build_index,
                **kwargs)
            handler.Build(renderer)


class TestManageRepository(testlib.DisabledTest):
    """Dont run automated tests for this tool."""