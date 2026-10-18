import os
import re
import tempfile
from qt.core import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextEdit, QComboBox, QProgressBar,
    QTabWidget, QWidget, QCheckBox, QFileDialog, QThread, pyqtSignal, QIcon,
    QTableWidget, QTableWidgetItem, QHeaderView, QListWidget, QListWidgetItem,
    QPixmap, Qt
)
from calibre.gui2.actions import InterfaceAction
from calibre.gui2 import error_dialog, info_dialog, question_dialog
from calibre.utils.config import JSONConfig

prefs = JSONConfig('plugins/book_translator_ai')
prefs.defaults['api_key'] = ''
prefs.defaults['modelo'] = ''
prefs.defaults['modelo_img'] = ''
prefs.defaults['modelos_texto'] = []
prefs.defaults['modelos_img'] = []
prefs.defaults['modo'] = 'Gratuito (Pausa 6s)'
prefs.defaults['tipo_libro'] = 'Novela Ligera / Manga / Fantasía'
prefs.defaults['desactivar_ssl'] = False


class HiloGenerarGuia(QThread):
    terminado = pyqtSignal(bool, str)
    estado = pyqtSignal(str)

    def __init__(self, api_key, modelo, ruta_epub, tipo_libro, guia_previa, desactivar_ssl):
        super().__init__()
        self.api_key = api_key
        self.modelo = modelo
        self.ruta_epub = ruta_epub
        self.tipo_libro = tipo_libro
        self.guia_previa = guia_previa
        self.desactivar_ssl = desactivar_ssl
        self._detener = False

    def solicitar_parada(self):
        self._detener = True

    def debe_detener(self):
        return self._detener

    def run(self):
        from calibre_plugins.book_translator_ai.engine import generar_guia_analisis
        try:
            guia = generar_guia_analisis(
                self.api_key, self.modelo, self.ruta_epub, self.tipo_libro,
                self.guia_previa, self.estado.emit, self.debe_detener, self.desactivar_ssl
            )
            self.terminado.emit(True, guia)
        except InterruptedError:
            self.terminado.emit(False, "Generación de guía cancelada por el usuario.")
        except Exception as e:
            self.terminado.emit(False, str(e))


class HiloTraducirImagen(QThread):
    terminado = pyqtSignal(bool, bytes, str)

    def __init__(self, api_key, bytes_orig, ext, prompt, modelo_img, desactivar_ssl):
        super().__init__()
        self.api_key = api_key
        self.bytes_orig = bytes_orig
        self.ext = ext
        self.prompt = prompt
        self.modelo_img = modelo_img
        self.desactivar_ssl = desactivar_ssl

    def run(self):
        from calibre_plugins.book_translator_ai.engine import traducir_imagen_con_gemini_api
        try:
            nuevos_bytes = traducir_imagen_con_gemini_api(
                self.api_key, self.bytes_orig, self.ext, self.prompt,
                modelo_img=self.modelo_img, desactivar_ssl=self.desactivar_ssl
            )
            self.terminado.emit(True, nuevos_bytes, "Imagen traducida exitosamente.")
        except Exception as e:
            self.terminado.emit(False, b"", str(e))


class HiloTraduccionSelectiva(QThread):
    progreso = pyqtSignal(int, int, str)
    terminado = pyqtSignal(bool, str, list)

    def __init__(self, api_key, modelo, ruta_epub, guia, pausa, archivos_seleccionados, desactivar_ssl):
        super().__init__()
        self.api_key = api_key
        self.modelo = modelo
        self.ruta_epub = ruta_epub
        self.guia = guia
        self.pausa = pausa
        self.archivos_seleccionados = archivos_seleccionados
        self.desactivar_ssl = desactivar_ssl
        self._detener = False

    def solicitar_parada(self):
        self._detener = True

    def debe_detener(self):
        return self._detener

    def run(self):
        from calibre_plugins.book_translator_ai.engine import traducir_capitulos_seleccionados
        try:
            errores = traducir_capitulos_seleccionados(
                self.progreso.emit,
                self.debe_detener,
                self.api_key, self.modelo, self.ruta_epub, self.guia,
                self.pausa, self.archivos_seleccionados, self.desactivar_ssl
            )
            if self._detener:
                self.terminado.emit(True, "PAUSADO", errores)
            else:
                self.terminado.emit(True, "COMPLETADO", errores)
        except InterruptedError:
            self.terminado.emit(True, "PAUSADO", [])
        except Exception as e:
            self.terminado.emit(False, str(e), [])


class DialogoTraductor(QDialog):
    def __init__(self, gui, libro_id, ruta_epub, titulo_libro):
        super().__init__(gui)
        self.gui = gui
        self.libro_id = libro_id
        self.ruta_epub = ruta_epub
        self.titulo_libro = titulo_libro
        self.guia_previa_texto = ""
        self.ruta_nueva_portada = ""
        self.datos_capitulos = []
        self.datos_imagenes = []

        self.setWindowTitle(f"Book Translator AI — {titulo_libro}")
        self.resize(880, 680)
        self.iniciar_ui()
        self.cargar_guia_existente_auto()
        self.refrescar_tabla_capitulos()
        self.cargar_lista_imagenes()

    def actualizar_listas_modelos_api(self):
        key = self.input_key.text().strip()
        if not key:
            error_dialog(self, "API Key requerida", "Ingresa tu API Key primero.", show=True)
            return
        from calibre_plugins.book_translator_ai.engine import obtener_modelos_disponibles_api
        self.lbl_estado.setText("Consultando modelos disponibles en Google AI Studio...")
        self.lbl_estado.repaint()
        try:
            textos, imgs = obtener_modelos_disponibles_api(key, self.chk_ssl.isChecked())
        except Exception as e:
            self.lbl_estado.setText("No se pudo actualizar la lista de modelos.")
            error_dialog(self, "Modelos", str(e), show=True)
            return
        act_t = self.combo_modelo.currentText()
        act_i = self.combo_modelo_img.currentText()
        self._llenar_combo(self.combo_modelo, textos, act_t if act_t in textos else "")
        self._llenar_combo(self.combo_modelo_img, imgs, act_i if act_i in imgs else "")
        prefs['modelos_texto'] = textos
        prefs['modelos_img'] = imgs
        self.guardar_preferencias()
        self.lbl_estado.setText(
            f"Lista actualizada desde la API: {len(textos)} modelos de texto, {len(imgs)} de imagen."
        )

    @staticmethod
    def _llenar_combo(combo, lista, actual):
        combo.clear()
        items = list(lista)
        if actual and actual not in items:
            items.insert(0, actual)
        combo.addItems(items)
        if actual:
            combo.setCurrentText(actual)
    
    def _hay_hilos_activos(self):
        for n in ("hilo_traduccion", "hilo_guia", "hilo_img"):
            h = getattr(self, n, None)
            if h is not None and h.isRunning():
                return True
        return False

    def reject(self):
        if self._hay_hilos_activos():
            info_dialog(self, "Proceso en curso",
                        "Hay una operación en curso. Pulsa «Pausar de forma segura» y espera a que termine antes de cerrar.",
                        show=True)
            return
        super().reject()

    def iniciar_ui(self):
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)    

        # ======================================================================
        # PESTAÑA 1: GESTOR DE CAPÍTULOS Y DIAGNÓSTICO
        # ======================================================================
        tab_caps = QWidget()
        layout_caps = QVBoxLayout(tab_caps)

        lbl_intro = QLabel(
            "<b>Estado de Capítulos del Libro (en orden de lectura &lt;spine&gt;):</b> "
            "El avance se guarda bloque por bloque fuera de la carpeta de la biblioteca de Calibre."
        )
        lbl_intro.setWordWrap(True)
        layout_caps.addWidget(lbl_intro)

        h_sel = QHBoxLayout()
        btn_sel_incompletos = QPushButton("☑️ Seleccionar Pendientes / A medias")
        btn_sel_incompletos.clicked.connect(self.seleccionar_pendientes)
        h_sel.addWidget(btn_sel_incompletos)

        btn_sel_todos = QPushButton("✅ Marcar Todos")
        btn_sel_todos.clicked.connect(lambda: self.marcar_todos(True))
        h_sel.addWidget(btn_sel_todos)

        btn_desel_todos = QPushButton("⬜ Desmarcar Todos")
        btn_desel_todos.clicked.connect(lambda: self.marcar_todos(False))
        h_sel.addWidget(btn_desel_todos)

        btn_refrescar = QPushButton("🔄 Verificar Estado")
        btn_refrescar.clicked.connect(self.refrescar_tabla_capitulos)
        h_sel.addWidget(btn_refrescar)
        layout_caps.addLayout(h_sel)

        self.tabla_caps = QTableWidget(0, 4)
        self.tabla_caps.setHorizontalHeaderLabels([
            "Traducir", "Archivo / Capítulo", "Palabras (Orig. → Trad.)", "Diagnóstico"
        ])
        header = self.tabla_caps.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout_caps.addWidget(self.tabla_caps)

        self.tabs.addTab(tab_caps, "1. Capítulos y Diagnóstico")

        # ======================================================================
        # PESTAÑA 2: GUÍA DE TRADUCCIÓN
        # ======================================================================
        tab_guia = QWidget()
        layout_guia = QVBoxLayout(tab_guia)

        h_tipo = QHBoxLayout()
        h_tipo.addWidget(QLabel("Tipo de libro:"))
        self.combo_tipo = QComboBox()
        self.combo_tipo.addItems([
            "Novela Ligera / Manga / Fantasía",
            "Técnico / Medicina e Informática Clínica",
            "Novela General / Ficción Narrativa",
            "Ensayo / No Ficción / Divulgación"
        ])
        self.combo_tipo.setCurrentText(prefs['tipo_libro'])
        h_tipo.addWidget(self.combo_tipo)
        layout_guia.addLayout(h_tipo)

        h_serie = QHBoxLayout()
        self.chk_serie = QCheckBox("Es continuación de saga/serie (heredar guía del volumen anterior)")
        self.chk_serie.toggled.connect(self.toggle_serie)
        h_serie.addWidget(self.chk_serie)

        self.btn_cargar_guia_previa = QPushButton("Cargar Guía previa (.txt)")
        self.btn_cargar_guia_previa.setEnabled(False)
        self.btn_cargar_guia_previa.clicked.connect(self.cargar_guia_previa_click)
        h_serie.addWidget(self.btn_cargar_guia_previa)

        self.lbl_guia_previa = QLabel("")
        h_serie.addWidget(self.lbl_guia_previa)
        h_serie.addStretch()
        layout_guia.addLayout(h_serie)

        self.btn_generar_guia = QPushButton("🔍 Analizar libro completo y generar Guía de Traducción")
        self.btn_generar_guia.clicked.connect(self.generar_guia_click)
        layout_guia.addWidget(self.btn_generar_guia)

        layout_guia.addWidget(QLabel("Editor de Guía de Traducción activa:"))
        self.editor_guia = QTextEdit()
        self.editor_guia.setPlaceholderText("Genera o importa una Guía de Traducción aquí...")
        layout_guia.addWidget(self.editor_guia)

        h_io_guia = QHBoxLayout()
        self.btn_guardar_guia = QPushButton("Guardar Guía como .txt")
        self.btn_guardar_guia.clicked.connect(self.guardar_guia_archivo)
        h_io_guia.addWidget(self.btn_guardar_guia)

        self.btn_cargar_guia_existente = QPushButton("Importar Guía desde .txt")
        self.btn_cargar_guia_existente.clicked.connect(self.importar_guia_archivo)
        h_io_guia.addWidget(self.btn_cargar_guia_existente)
        layout_guia.addLayout(h_io_guia)

        self.tabs.addTab(tab_guia, "2. Guía de Traducción")

        # ======================================================================
        # PESTAÑA 3: GESTOR INTEGRAL DE IMÁGENES Y PORTADAS
        # ======================================================================
        tab_portada = QWidget()
        layout_portada = QHBoxLayout(tab_portada)

        col_izq = QVBoxLayout()
        col_izq.addWidget(QLabel("<b>Imágenes del EPUB (Portada, Mapas, Extras):</b>"))
        self.lista_imgs = QListWidget()
        self.lista_imgs.currentRowChanged.connect(self.seleccionar_imagen_lista)
        col_izq.addWidget(self.lista_imgs)
        layout_portada.addLayout(col_izq, 1)

        col_der = QVBoxLayout()
        self.lbl_preview = QLabel("Selecciona una imagen de la lista")
        self.lbl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview.setMinimumSize(280, 320)
        self.lbl_preview.setStyleSheet("border: 1px solid #aaa; background-color: #222; color: #ccc;")
        col_der.addWidget(self.lbl_preview, 1)

        col_der.addWidget(QLabel("Instrucción para traducir la imagen seleccionada:"))
        self.txt_prompt_portada = QTextEdit()
        self.txt_prompt_portada.setMaximumHeight(65)
        self.txt_prompt_portada.setPlainText(
            "Genera una copia exacta de esta imagen traduciendo todos los textos en inglés al español, "
            "manteniendo intacta la ilustración, el diseño y la tipografía original."
        )
        col_der.addWidget(self.txt_prompt_portada)

        self.btn_traducir_img_ia = QPushButton("🪄 Traducir Imagen Seleccionada directamente con Gemini")
        self.btn_traducir_img_ia.clicked.connect(self.traducir_imagen_directo_click)
        col_der.addWidget(self.btn_traducir_img_ia)

        h_manual_img = QHBoxLayout()
        self.btn_exportar_img = QPushButton("📥 Exportar imagen al Escritorio")
        self.btn_exportar_img.clicked.connect(self.exportar_imagen_seleccionada_click)
        h_manual_img.addWidget(self.btn_exportar_img)

        self.btn_reemplazar_img = QPushButton("🖼️ Cargar reemplazo desde archivo")
        self.btn_reemplazar_img.clicked.connect(self.reemplazar_imagen_manual_click)
        h_manual_img.addWidget(self.btn_reemplazar_img)
        col_der.addLayout(h_manual_img)

        layout_portada.addLayout(col_der, 1)
        self.tabs.addTab(tab_portada, "3. Portada e Imágenes")

        # ======================================================================
        # PESTAÑA 4: CONFIGURACIÓN API
        # ======================================================================
        tab_ajustes = QWidget()
        layout_ajustes = QVBoxLayout(tab_ajustes)

        layout_ajustes.addWidget(QLabel("API Key (Google AI Studio):"))
        self.input_key = QLineEdit()
        self.input_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_key.setText(prefs['api_key'])
        layout_ajustes.addWidget(self.input_key)

        # --- MODELO DE TEXTO (Dinámico) ---
        layout_ajustes.addWidget(QLabel("Modelo de traducción de texto:"))
        self.combo_modelo = QComboBox()
        self._llenar_combo(self.combo_modelo, prefs['modelos_texto'], prefs['modelo'])
        layout_ajustes.addWidget(self.combo_modelo)

        # --- MODELO DE IMAGEN (Dinámico) ---
        layout_ajustes.addWidget(QLabel("Modelo de edición de imágenes:"))
        self.combo_modelo_img = QComboBox()
        self._llenar_combo(self.combo_modelo_img, prefs['modelos_img'], prefs['modelo_img'])
        layout_ajustes.addWidget(self.combo_modelo_img)

        # Botón para refrescar los modelos desde la API
        self.btn_cargar_modelos = QPushButton("🔄 Actualizar lista de modelos desde Google")
        self.btn_cargar_modelos.clicked.connect(self.actualizar_listas_modelos_api)
        layout_ajustes.addWidget(self.btn_cargar_modelos)

        layout_ajustes.addWidget(QLabel("Régimen de Cuota:"))
        self.combo_modo = QComboBox()
        self.combo_modo.addItems(["Gratuito (Pausa 6s)", "Pago / Tier 1 (Rápido)"])
        self.combo_modo.setCurrentText(prefs['modo'])
        layout_ajustes.addWidget(self.combo_modo)

        self.chk_ssl = QCheckBox("Desactivar verificación de certificado SSL (no recomendado; usar solo ante error de proxy/antivirus local)")
        self.chk_ssl.setChecked(bool(prefs['desactivar_ssl']))
        layout_ajustes.addWidget(self.chk_ssl)

        layout_ajustes.addStretch()
        self.tabs.addTab(tab_ajustes, "4. Configuración API")

        # ======================================================================
        # BARRA DE PROGRESO Y BOTONES DE ACCIÓN
        # ======================================================================
        self.lbl_estado = QLabel("Listo.")
        layout.addWidget(self.lbl_estado)

        self.progreso_bar = QProgressBar()
        self.progreso_bar.setValue(0)
        layout.addWidget(self.progreso_bar)

        h_botones = QHBoxLayout()
        self.btn_iniciar = QPushButton("🚀 Traducir Capítulos Seleccionados")
        self.btn_iniciar.clicked.connect(self.iniciar_traduccion_click)
        h_botones.addWidget(self.btn_iniciar)

        self.btn_detener = QPushButton("⏹️ Pausar de forma segura")
        self.btn_detener.setEnabled(False)
        self.btn_detener.clicked.connect(self.detener_traduccion_click)
        h_botones.addWidget(self.btn_detener)

        self.btn_compilar = QPushButton("📚 Compilar y Agregar a Calibre")
        self.btn_compilar.clicked.connect(self.compilar_y_catalogar_click)
        h_botones.addWidget(self.btn_compilar)

        self.btn_cerrar = QPushButton("Cerrar")
        self.btn_cerrar.clicked.connect(self.reject)
        h_botones.addWidget(self.btn_cerrar)
        layout.addLayout(h_botones)

    # --- Funciones del Gestor de Capítulos ---
    def cargar_guia_existente_auto(self):
        from calibre_plugins.book_translator_ai.engine import obtener_ruta_guia_auto
        ruta_auto = obtener_ruta_guia_auto(self.ruta_epub)
        if os.path.exists(ruta_auto):
            try:
                with open(ruta_auto, "r", encoding="utf-8") as f:
                    self.editor_guia.setPlainText(f.read().strip())
                self.lbl_estado.setText("Guía previa cargada automáticamente.")
            except Exception:
                pass

    def refrescar_tabla_capitulos(self):
        from calibre_plugins.book_translator_ai.engine import diagnosticar_capitulos_epub
        self.datos_capitulos = diagnosticar_capitulos_epub(self.ruta_epub)
        self.tabla_caps.setRowCount(len(self.datos_capitulos))

        completos = 0
        for fila, cap in enumerate(self.datos_capitulos):
            item_chk = QTableWidgetItem()
            item_chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            item_chk.setCheckState(Qt.CheckState.Checked if cap["seleccionar"] else Qt.CheckState.Unchecked)
            self.tabla_caps.setItem(fila, 0, item_chk)

            item_nom = QTableWidgetItem(cap["nombre"])
            item_nom.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tabla_caps.setItem(fila, 1, item_nom)

            texto_pal = f"{cap['palabras_orig']} → {cap['palabras_trad']}"
            item_pal = QTableWidgetItem(texto_pal)
            item_pal.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tabla_caps.setItem(fila, 2, item_pal)

            item_est = QTableWidgetItem(cap["estado"])
            item_est.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tabla_caps.setItem(fila, 3, item_est)

            if cap["estado"].startswith("✅") or cap["estado"].startswith("🔍"):
                completos += 1

        self.lbl_estado.setText(
            f"Diagnóstico: {completos} de {len(self.datos_capitulos)} capítulos narrativos traducidos."
        )

    def seleccionar_pendientes(self):
        for fila, cap in enumerate(self.datos_capitulos):
            marcar = not (cap["estado"].startswith("✅") or cap["estado"].startswith("🔍"))
            self.tabla_caps.item(fila, 0).setCheckState(
                Qt.CheckState.Checked if marcar else Qt.CheckState.Unchecked
            )

    def marcar_todos(self, marcar: bool):
        estado_qt = Qt.CheckState.Checked if marcar else Qt.CheckState.Unchecked
        for fila in range(self.tabla_caps.rowCount()):
            self.tabla_caps.item(fila, 0).setCheckState(estado_qt)

    def obtener_archivos_marcados(self) -> list[str]:
        seleccionados = []
        for fila in range(self.tabla_caps.rowCount()):
            if self.tabla_caps.item(fila, 0).checkState() == Qt.CheckState.Checked:
                seleccionados.append(self.datos_capitulos[fila]["nombre"])
        return seleccionados

    # --- Funciones del Gestor de Imágenes ---
    def cargar_lista_imagenes(self):
        from calibre_plugins.book_translator_ai.engine import listar_imagenes_epub
        self.datos_imagenes = listar_imagenes_epub(self.ruta_epub)
        self.lista_imgs.clear()
        fila_portada = 0

        for idx, info in enumerate(self.datos_imagenes):
            etiqueta = info["nombre"]
            if info["es_portada"]:
                etiqueta = f"⭐ [PORTADA] {etiqueta}"
                fila_portada = idx
            if info["traducida"]:
                etiqueta = f"✅ {etiqueta} (Traducida)"
            else:
                etiqueta = f"🖼️ {etiqueta} ({info['tamano_kb']} KB)"
            self.lista_imgs.addItem(QListWidgetItem(etiqueta))

        if self.datos_imagenes:
            self.lista_imgs.setCurrentRow(fila_portada)

    def seleccionar_imagen_lista(self, fila: int):
        if fila < 0 or fila >= len(self.datos_imagenes):
            return
        from calibre_plugins.book_translator_ai.engine import leer_bytes_imagen_epub
        nombre_interno = self.datos_imagenes[fila]["nombre"]
        try:
            datos_bytes = leer_bytes_imagen_epub(self.ruta_epub, nombre_interno, preferir_traducida=True)
            pix = QPixmap()
            pix.loadFromData(datos_bytes)
            if not pix.isNull():
                pix_escalado = pix.scaled(
                    280, 340, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
                self.lbl_preview.setPixmap(pix_escalado)
            else:
                self.lbl_preview.setText("No se pudo previsualizar la imagen.")
        except Exception as e:
            self.lbl_preview.setText(f"Error: {e}")

    def traducir_imagen_directo_click(self):
        fila = self.lista_imgs.currentRow()
        if fila < 0:
            return
        key = self.input_key.text().strip()
        if not key:
            error_dialog(self, "API Key requerida", "Configura tu API Key en la pestaña 4.", show=True)
            return

        self.guardar_preferencias()

        modelo_imagen = self.combo_modelo_img.currentText().strip()

        from calibre_plugins.book_translator_ai.engine import leer_bytes_imagen_epub
        info_img = self.datos_imagenes[fila]
        nombre_interno = info_img["nombre"]
        ext = os.path.splitext(nombre_interno)[1] or ".jpg"
        bytes_orig = leer_bytes_imagen_epub(self.ruta_epub, nombre_interno, preferir_traducida=False)
        prompt = self.txt_prompt_portada.toPlainText().strip()

        self.btn_traducir_img_ia.setEnabled(False)
        self.lbl_estado.setText(f"🪄 Enviando '{os.path.basename(nombre_interno)}' a Gemini...")

        self.hilo_img = HiloTraducirImagen(
            key, bytes_orig, ext, prompt,
            modelo_imagen,
            self.chk_ssl.isChecked()
        )
        self.hilo_img.terminado.connect(lambda exito, b, msg: self.fin_traducir_imagen(fila, exito, b, msg))
        self.hilo_img.start()

    def fin_traducir_imagen(self, fila: int, exito: bool, nuevos_bytes: bytes, mensaje: str):
        self.btn_traducir_img_ia.setEnabled(True)
        if exito and nuevos_bytes:
            from calibre_plugins.book_translator_ai.engine import guardar_imagen_traducida_disco
            nombre_interno = self.datos_imagenes[fila]["nombre"]
            ruta_guardada = guardar_imagen_traducida_disco(self.ruta_epub, nombre_interno, nuevos_bytes)
            if self.datos_imagenes[fila]["es_portada"]:
                self.ruta_nueva_portada = ruta_guardada
            self.cargar_lista_imagenes()
            self.lista_imgs.setCurrentRow(fila)
            self.lbl_estado.setText(f"✓ Imagen '{os.path.basename(nombre_interno)}' traducida.")
            info_dialog(self, "Imagen Traducida", "La imagen fue traducida y convertida al formato original del EPUB.", show=True)
        else:
            self.lbl_estado.setText("No se pudo traducir la imagen directamente por API.")
            error_dialog(self, "Aviso de API de Imagen", mensaje, show=True)

    def exportar_imagen_seleccionada_click(self):
        fila = self.lista_imgs.currentRow()
        if fila < 0:
            return
        from calibre_plugins.book_translator_ai.engine import leer_bytes_imagen_epub
        nombre_interno = self.datos_imagenes[fila]["nombre"]
        bytes_img = leer_bytes_imagen_epub(self.ruta_epub, nombre_interno, preferir_traducida=False)

        escritorio = os.path.join(os.path.expanduser("~"), "Desktop")
        nombre_limpio = os.path.basename(nombre_interno)
        ruta_guardado = os.path.join(escritorio, f"original_{nombre_limpio}")
        with open(ruta_guardado, "wb") as f:
            f.write(bytes_img)
        info_dialog(self, "Imagen Exportada", f"Guardada en tu Escritorio:\n{ruta_guardado}", show=True)

    def reemplazar_imagen_manual_click(self):
        fila = self.lista_imgs.currentRow()
        if fila < 0:
            return
        ruta, _ = QFileDialog.getOpenFileName(
            self, "Seleccionar imagen traducida", "", "Imágenes (*.jpg *.jpeg *.png *.webp)"
        )
        if ruta:
            from calibre_plugins.book_translator_ai.engine import guardar_imagen_traducida_disco
            with open(ruta, "rb") as f:
                nuevos_bytes = f.read()
            nombre_interno = self.datos_imagenes[fila]["nombre"]
            ruta_guardada = guardar_imagen_traducida_disco(self.ruta_epub, nombre_interno, nuevos_bytes)
            if self.datos_imagenes[fila]["es_portada"]:
                self.ruta_nueva_portada = ruta_guardada
            self.cargar_lista_imagenes()
            self.lista_imgs.setCurrentRow(fila)
            self.lbl_estado.setText(f"✓ Imagen '{os.path.basename(nombre_interno)}' reemplazada.")

    # --- Funciones de Guía ---
    def toggle_serie(self, activo):
        self.btn_cargar_guia_previa.setEnabled(activo)
        if not activo:
            self.guia_previa_texto = ""
            self.lbl_guia_previa.setText("")

    def cargar_guia_previa_click(self):
        ruta, _ = QFileDialog.getOpenFileName(self, "Seleccionar Guía previa", "", "Archivos de Texto (*.txt)")
        if ruta:
            with open(ruta, "r", encoding="utf-8") as f:
                self.guia_previa_texto = f.read()
            self.lbl_guia_previa.setText(f"✓ {os.path.basename(ruta)}")

    def guardar_guia_archivo(self):
        contenido = self.editor_guia.toPlainText().strip()
        if not contenido:
            error_dialog(self, "Guía vacía", "No hay directivas de guía para exportar.", show=True)
            return
        escritorio = os.path.join(os.path.expanduser("~"), "Desktop")
        nombre_base, _ = os.path.splitext(os.path.basename(self.ruta_epub))
        ruta_sugerida = os.path.join(escritorio, f"guia_{nombre_base}.txt")
        ruta, _ = QFileDialog.getSaveFileName(self, "Guardar Guía como...", ruta_sugerida, "Archivos de Texto (*.txt)")
        if ruta:
            with open(ruta, "w", encoding="utf-8") as f:
                f.write(contenido)
            info_dialog(self, "Guardado", f"Guía guardada en:\n{ruta}", show=True)

    def importar_guia_archivo(self):
        ruta, _ = QFileDialog.getOpenFileName(self, "Seleccionar Guía", "", "Archivos de Texto (*.txt)")
        if ruta:
            with open(ruta, "r", encoding="utf-8") as f:
                self.editor_guia.setPlainText(f.read())

    def guardar_preferencias(self):
        prefs['api_key'] = self.input_key.text().strip()
        prefs['modelo'] = self.combo_modelo.currentText().strip()
        prefs['modelo_img'] = self.combo_modelo_img.currentText().strip()
        prefs['modo'] = self.combo_modo.currentText()
        prefs['tipo_libro'] = self.combo_tipo.currentText()
        prefs['desactivar_ssl'] = self.chk_ssl.isChecked()

    def generar_guia_click(self):
        key = self.input_key.text().strip()
        if not key:
            error_dialog(self, "API Key requerida", "Configura tu API Key en la pestaña '4. Configuración API'.", show=True)
            return
        if not self.combo_modelo.currentText().strip():
            error_dialog(self, "Modelo requerido",
                         "Pulsa 'Actualizar lista de modelos' en la pestaña 4 y elige un modelo.", show=True)
            return

        self.guardar_preferencias()
        self.btn_generar_guia.setEnabled(False)
        self.btn_detener.setEnabled(True)
        self.lbl_estado.setText("Extrayendo texto del libro y analizando con Gemini...")

        self.hilo_guia = HiloGenerarGuia(
            key, self.combo_modelo.currentText().strip(), self.ruta_epub,
            self.combo_tipo.currentText(), self.guia_previa_texto, self.chk_ssl.isChecked()
        )
        self.hilo_guia.estado.connect(self.lbl_estado.setText)
        self.hilo_guia.terminado.connect(self.fin_generar_guia)
        self.hilo_guia.start()

    def fin_generar_guia(self, exito, resultado):
        self.btn_generar_guia.setEnabled(True)
        self.btn_detener.setEnabled(False)
        if exito:
            self.editor_guia.setPlainText(resultado)
            self.lbl_estado.setText("Guía generada y guardada automáticamente.")
            info_dialog(self, "Guía Lista", "Guía generada y guardada.", show=True)
        else:
            self.lbl_estado.setText("Proceso de guía detenido o con error.")
            error_dialog(self, "Aviso", f"{resultado}", show=True)

    # --- Ejecución de Traducción y Compilación ---
    def iniciar_traduccion_click(self):
        key = self.input_key.text().strip()
        guia = self.editor_guia.toPlainText().strip()
        archivos = self.obtener_archivos_marcados()

        if not key:
            error_dialog(self, "API Key requerida", "Ingresa tu API Key en la pestaña '4. Configuración API'.", show=True)
            return
        if not guia:
            error_dialog(self, "Guía requerida", "Ve a la pestaña '2. Guía de Traducción' primero.", show=True)
            self.tabs.setCurrentIndex(1)
            return
        if not archivos:
            info_dialog(self, "Sin selección", "No hay ningún capítulo marcado para traducir.", show=True)
            return
        estados = {c["nombre"]: c["estado"] for c in self.datos_capitulos}
        ya = [a for a in archivos if estados.get(a, "").startswith(("✅", "🔍"))]
        if ya and not question_dialog(
            self, "Capítulos ya traducidos",
            f"{len(ya)} de los capítulos marcados ya están traducidos y volverán a cobrarse en la API. ¿Continuar?",
            default_yes=False
        ):
            return        

        self.guardar_preferencias()
        from calibre_plugins.book_translator_ai.engine import obtener_ruta_guia_auto
        ruta_g = obtener_ruta_guia_auto(self.ruta_epub)
        with open(ruta_g + ".tmp", "w", encoding="utf-8") as f:
            f.write(guia)
        os.replace(ruta_g + ".tmp", ruta_g)

        pausa_seg = 6 if "Gratuito" in self.combo_modo.currentText() else 1

        self.btn_iniciar.setEnabled(False)
        self.btn_compilar.setEnabled(False)
        self.btn_detener.setEnabled(True)

        self.hilo_traduccion = HiloTraduccionSelectiva(
            key, self.combo_modelo.currentText().strip(), self.ruta_epub,
            guia, pausa_seg, archivos, self.chk_ssl.isChecked()
        )
        self.hilo_traduccion.progreso.connect(self.actualizar_progreso)
        self.hilo_traduccion.terminado.connect(self.fin_traduccion)
        self.hilo_traduccion.start()

    def detener_traduccion_click(self):
        self.btn_detener.setEnabled(False)
        self.lbl_estado.setText("⏳ Deteniendo de forma segura...")
        if hasattr(self, 'hilo_traduccion') and self.hilo_traduccion.isRunning():
            self.hilo_traduccion.solicitar_parada()
        if hasattr(self, 'hilo_guia') and self.hilo_guia.isRunning():
            self.hilo_guia.solicitar_parada()

    def actualizar_progreso(self, actual, total, mensaje):
        self.progreso_bar.setMaximum(total)
        self.progreso_bar.setValue(actual)
        self.lbl_estado.setText(f"[{actual}/{total}] {mensaje}")

    def fin_traduccion(self, exito, mensaje, errores):
        self.btn_iniciar.setEnabled(True)
        self.btn_compilar.setEnabled(True)
        self.btn_detener.setEnabled(False)
        self.refrescar_tabla_capitulos()
        from calibre_plugins.book_translator_ai.engine import resumen_uso
        self.lbl_estado.setText(f"{self.lbl_estado.text()} | {resumen_uso()}")

        if exito and mensaje == "PAUSADO":
            info_dialog(self, "Traducción Pausada", "El avance por bloques quedó guardado de forma segura.", show=True)
        elif exito:
            if errores:
                detalle = "\n".join(f"• {nom}: {err}" for nom, err in errores)
                error_dialog(
                    self, "Lote completado con errores parciales",
                    f"Algunos capítulos no pudieron completarse y quedaron marcados para reintentar:\n\n{detalle}",
                    show=True
                )
            else:
                info_dialog(self, "Selección completada",
                            "Se tradujeron todos los capítulos seleccionados. Cuando estés listo, "
                            "pulsa «Compilar y Agregar a Calibre».", show=True)
        else:
            error_dialog(self, "Interrupción Fatal", f"Se detuvo el proceso:\n{mensaje}", show=True)

    def compilar_y_catalogar_click(self):
        from calibre_plugins.book_translator_ai.engine import compilar_epub_traducido
        fd, ruta_salida_temp = tempfile.mkstemp(suffix=".epub")
        os.close(fd)

        try:
            self.lbl_estado.setText("Compilando EPUB y catalogando en Calibre...")
            traducidos, totales, portada_final = compilar_epub_traducido(
                self.ruta_epub, ruta_salida_temp, self.ruta_nueva_portada
            )

            db = self.gui.current_db.new_api
            mi = db.get_metadata(self.libro_id)
            mi.title = f"{self.titulo_libro} (Español)"
            if totales > 0 and traducidos == totales:
                mi.languages = ['spa']

            if portada_final and os.path.exists(portada_final):
                ext_img = os.path.splitext(portada_final)[1].lstrip('.').lower() or 'jpg'
                with open(portada_final, 'rb') as f:
                    mi.cover_data = (ext_img, f.read())

            ids, _ = db.add_books([(mi, {'EPUB': ruta_salida_temp})])
            self.gui.library_view.model().books_added(len(ids))
            try:
                self.gui.library_view.model().refresh()
            except Exception:
                pass

            self.lbl_estado.setText(f"¡Libro agregado a Calibre! ({traducidos}/{totales} capítulos traducidos).")
            info_dialog(
                self,
                "¡Libro Catalogado en Calibre!",
                f"Se compiló y agregó a tu biblioteca:\n\n"
                f"📖 '{mi.title}'\n"
                f"📊 Capítulos traducidos: {traducidos} de {totales}.",
                show=True
            )
        except Exception as e:
            error_dialog(self, "Error al compilar", f"No se pudo agregar a Calibre:\n{e}", show=True)
        finally:
            if os.path.exists(ruta_salida_temp):
                try:
                    os.remove(ruta_salida_temp)
                except OSError:
                    pass


class BookTranslatorAIAction(InterfaceAction):
    name = 'Book Translator AI'
    action_spec = ('Book Translator AI', 'languages.png', 'Traducir libro seleccionado con Book Translator AI', None)

    def genesis(self):
        self.qaction.setIcon(QIcon.ic('languages.png'))
        self.qaction.triggered.connect(self.ejecutar)

    def ejecutar(self):
        ids_seleccionados = self.gui.library_view.get_selected_ids()
        if not ids_seleccionados:
            error_dialog(
                self.gui,
                "Sin selección",
                "Por favor selecciona un libro en tu biblioteca de Calibre antes de abrir el traductor.",
                show=True
            )
            return

        libro_id = ids_seleccionados[0]
        db = self.gui.current_db.new_api

        if not db.has_format(libro_id, 'EPUB'):
            error_dialog(
                self.gui,
                "Formato no válido",
                "El libro seleccionado no tiene formato EPUB en la biblioteca.",
                show=True
            )
            return

        ruta_epub = db.format_abspath(libro_id, 'EPUB')

        try:
            titulo = db.field_for('title', libro_id) or "Libro sin título"
        except Exception:
            titulo = self.gui.current_db.title(libro_id, index_is_id=True)

        dialogo = DialogoTraductor(self.gui, libro_id, ruta_epub, titulo)
        dialogo.exec()