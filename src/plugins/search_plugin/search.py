import os
import sqlite3
import struct
import time

from qgis.PyQt.QtCore import Qt, QObject, pyqtSignal, QThread, QEvent
from qgis.PyQt.QtWidgets import QListWidgetItem
from qgis.PyQt.uic import loadUiType
from qgis.core import QgsMapLayer, QgsProject, QgsFeatureRequest

import roam.api.utils
import roam.utils
from roam.api.events import RoamEvents
from roam.api.plugins import Page


def resolve(name):
    f = os.path.join(os.path.dirname(__file__), name)
    return f


widget, base = loadUiType(resolve("search.ui"))


def make_rank_func(weights):
    # Taken from http://chipaca.com/post/16877190061/doing-full-text-search-in-sqlite-from-python
    def rank(matchinfo):
        # matchinfo is defined as returning 32-bit unsigned integers
        # in machine byte order
        # http://www.sqlite.org/fts3.html#matchinfo
        # and struct defaults to machine byte order
        matchinfo = struct.unpack("I"*(len(matchinfo)/4), matchinfo)
        it = iter(matchinfo[2:])
        return sum(x[0]*w/x[1]
                   for x, w in zip(zip(it, it, it), weights)
                   if x[1])

    return rank


class IndexBuilder(QObject):
    indexBuilt = pyqtSignal(object, float)
    finished = pyqtSignal()

    def __init__(self, indexpath, indexconfig):
        super(IndexBuilder, self).__init__()
        self.indexpath = indexpath
        self.indexconfig = indexconfig

    def build_index(self):
        dbpath = os.path.join(self.indexpath, "index.db")
        self.conn = sqlite3.connect(dbpath)
        c = self.conn.cursor()
        c.execute("DROP TABLE IF EXISTS search")
        c.execute("DROP TABLE IF EXISTS featureinfo")
        c.execute("CREATE TABLE featureinfo(id INTEGER PRIMARY KEY, layer, featureid)")

        def get_columns():
            columns = set()
            for config in self.indexconfig.values():
                for c in config['columns']:
                    columns.add('"{}"'.format(c))
            return columns

        columns = ','.join(get_columns())
        c.execute("CREATE VIRTUAL TABLE search USING fts4({})".format(columns))

        def get_features():
            rowid = 0
            for layername, config in self.indexconfig.items():
                if layername == "_all":
                    layers = roam.api.utils.layers(layertype=QgsMapLayer.VectorLayer)
                    print(layers)
                    for layer in layers:
                        for count, layer, fid, data in get_data(layer, config, rowid):
                            rowid = count
                            yield count, layer, fid, data
                else:
                    try:
                        layer = roam.api.utils.layer_by_name(layername)
                    except IndexError:
                        continue
                    for count, layer, fid, data in get_data(layer, config, rowid):
                        rowid = count
                        yield count, layer, fid, data

        def get_data(layer, config, rowid):
            layerfields = [field.name() for field in layer.fields()]
            configfields = config['columns']
            # Pull out the fields that match on the layer and the config:
            fields = set(layerfields) & set(configfields)

            if not fields:
                return

            for feature in layer.getFeatures():
                data = {}
                for field in fields:
                    value = str(feature[field])
                    data['"{}"'.format(field)] = str("{}: {}").format(field, value)
                if not data:
                    continue
                fid = feature.id()
                rowid += 1
                yield rowid, layer.name(), fid, data

        start = time.time()
        for row in get_features():
            c.execute("INSERT INTO featureinfo(id, layer, featureid) VALUES(?, ?, ?)", (row[0], row[1], row[2]))
            data = row[3]
            # HACK
            fields = ",".join(data.keys())
            placeholders = "?," * (len(data.values()))
            placeholders = placeholders.strip(',')
            query = "INSERT INTO search(docid, {0}) VALUES({1}, {2})".format(fields, row[0], placeholders)
            c.execute(query, list(data.values()))

        self.conn.commit()
        self.conn.close()
        self.indexBuilt.emit(dbpath, time.time() - start)
        self.quit()

    def quit(self):
        self.conn.close()
        self.finished.emit()


def valid_search_settings(settings):
    try:
        settings = settings['search']
        for layerconfig in settings.values():
            columns = layerconfig['columns']
        return True, settings
    except KeyError:
        return False, {}


class SearchPlugin(widget, base, Page):
    title = "Search"
    icon = resolve("search.svg")

    def __init__(self, api, parent=None):
        super(SearchPlugin, self).__init__(parent)
        self.setupUi(self)
        self.api = api
        self.project = None
        self.dbpath = None
        self.searchbox.textChanged.connect(self.search)
        self.searchbox.installEventFilter(self)
        self.clearButton.pressed.connect(self.searchbox.clear)
        self.resultsView.itemClicked.connect(self.jump_to)
        self.rebuildLabel.linkActivated.connect(self.rebuild_index)
        self.fuzzyCheck.stateChanged.connect(self.fuzzy_changed)
        self.indexbuilder = None
        self.indexthread = None

        roam.api.utils.install_touch_scroll(self.resultsView)


    def fuzzy_changed(self, state):
        self.search(self.searchbox.text())

    def index_built(self, dbpath, timing):
        self.dbpath = dbpath
        self.resultsView.clear()
        self.searchbox.setEnabled(True)
        print("Index built in: {} seconds".format(timing))

    def eventFilter(self, object, event):
        if event.type() == QEvent.FocusIn:
            RoamEvents.openkeyboard.emit()
        return False

    @property
    def db(self):
        db = sqlite3.connect(self.dbpath)
        db.create_function("rank", 1, make_rank_func((1., .1, 0, 0)))
        return db

    def project_loaded(self, project):
        self.project = project
        self.build_index(project)

    def rebuild_index(self):
        self.build_index(self.project)

    def build_index(self, project):
        self.searchbox.setEnabled(False)
        self.resultsView.setEnabled(False)
        self.resultsView.addItem("building search index...")

        validformat, settings = valid_search_settings(project.settings)
        if not validformat:
            RoamEvents.raisemessage("Searching", "Invalid search config.", level=1)
            self.searchbox.hide()
            self.resultsView.clear()
            self.resultsView.addItem("Invalid search config found")
            return

        self.indexthread = QThread()
        path = os.path.join(os.environ['APPDATA'], "roam", project.name)

        roam.utils.info("Search index path: {0}".format(path))

        if not os.path.exists(path):
            os.makedirs(path)

        self.indexbuilder = IndexBuilder(path, settings)
        self.indexbuilder.moveToThread(self.indexthread)

        QgsProject.instance().removeAll.connect(self.indexthread.quit)

        self.indexbuilder.indexBuilt.connect(self.index_built)
        self.indexbuilder.finished.connect(self.indexthread.quit)
        self.indexthread.started.connect(self.indexbuilder.build_index)
        self.indexthread.finished.connect(self.indexbuilder.quit)

        self.indexthread.start()

    def search(self, text):
        db = self.db
        c = db.cursor()
        self.resultsView.clear()
        self.resultsView.setEnabled(False)
        if not text:
            return

        if self.fuzzyCheck.isChecked():
            search = "* ".join(text.split()) + "*"
        else:
            search = text
        query = c.execute("""SELECT layer, featureid, snippet(search, '[',']') as snippet
                            FROM search
                            JOIN featureinfo on search.docid = featureinfo.id
                            WHERE search match '{}' LIMIT 100""".format(search)).fetchall()
        for layer, featureid, snippet in query:
            item = QListWidgetItem()
            text = "{}\n {}".format(layer, snippet.replace('\n', ' '))
            item.setText(text)
            item.setData(Qt.UserRole + 1, (layer, featureid, snippet))
            self.resultsView.addItem(item)

        self.resultsView.setEnabled(True)

        if self.resultsView.count() == 0:
            self.resultsView.addItem("No Results")
            self.resultsView.setEnabled(False)
        db.close()

    def jump_to(self, item):
        data = item.data(Qt.UserRole + 1)
        if not data:
            return
        layername, fid = data[0], data[1]
        layer = roam.api.utils.layer_by_name(layername)
        feature = next(layer.getFeatures(QgsFeatureRequest(fid)))
        self.api.mainwindow.showmap()
        self.api.mainwindow.canvas.zoomToFeatureIds(layer, [fid])
        RoamEvents.selectionchanged.emit({layer: [feature]})
