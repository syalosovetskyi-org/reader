import { scrollPosition, visiblePages, fromFraction } from './fixed-scroll.js'
const parseViewport = str => str
    ?.split(/[,;\s]/) // NOTE: technically, only the comma is valid
    ?.filter(x => x)
    ?.map(x => x.split('=').map(x => x.trim()))

const getViewport = (doc, viewport) => {
    // use `viewBox` for SVG
    if (doc.documentElement.localName === 'svg') {
        const [, , width, height] = doc.documentElement
            .getAttribute('viewBox')?.split(/\s/) ?? []
        return { width, height }
    }

    // get `viewport` `meta` element
    const meta = parseViewport(doc.querySelector('meta[name="viewport"]')
        ?.getAttribute('content'))
    if (meta) return Object.fromEntries(meta)

    // fallback to book's viewport
    if (typeof viewport === 'string') return parseViewport(viewport)
    if (viewport?.width && viewport.height) return viewport

    // if no viewport (possibly with image directly in spine), get image size
    const img = doc.querySelector('img')
    if (img) return { width: img.naturalWidth, height: img.naturalHeight }

    // just show *something*, i guess...
    console.warn(new Error('Missing viewport properties'))
    return { width: 1000, height: 2000 }
}

export class FixedLayout extends HTMLElement {
    static observedAttributes = ['zoom', 'flow']
    #root = this.attachShadow({ mode: 'closed' })
    #observer = new ResizeObserver(() => this.#render())
    #spreads
    #strip
    #slots = []
    #frames = new Map()
    #scrollRAF
    #heights = []
    #stripHeight = 0
    #viewHeight = 0
    #position = { index: 0, offset: 0 }
    #generation = 0
    #loading
    #ready
    #frameController = new AbortController()
    get scrolled() { return this.getAttribute('flow') === 'scrolled' }
    get atEnd() {
        return this.scrolled
            // Высоты ленты известны: scrollHeight/clientHeight здесь заставляли бы
            // браузер пересчитывать раскладку на каждом relocate прокрутки.
            ? !!this.#strip && this.scrollTop + this.#viewHeight >= this.#stripHeight - 2
            : this.index >= this.book.sections.length - 1
    }
    #index = -1
    defaultViewport
    spread
    #portrait = false
    #left
    #right
    #center
    #side
    #zoom
    #scale = 1          // масштаб текущей страницы, как он вышел после раскладки
    #zoomKey = ''       // каким был #zoom при прошлой раскладке ленты
    #pinch = null
    pinchUntil = 0      // до этого момента касания — щипок, а не свайп (navigation.js)
    // Перерисовка страницы под масштаб. Новый масштаб заменяет frame.pending,
    // и прежняя отрисовка остаётся без обработчика: если книгу в это время
    // закрыли (pdf.js уже разобран), её отказ всплывал бы ошибкой страницы.
    // Отказ пишем в консоль с контекстом; сама страница перерисуется следующим вызовом.
    #zoomFrame(frame, doc, scale) {
        const p = frame.onZoom({ doc, scale })
        p?.catch?.(error => {
            if (frame.iframe?.isConnected) console.warn('PDF: страница не перерисовалась под масштаб', scale, error)
        })
        return p
    }
    // Масштаб, которым сейчас показана страница (единицы страницы → CSS px).
    get currentScale() { return this.#scale }
    // Ширина страницы ленты при текущем масштабе.
    #stripPageWidth(size, viewWidth, viewHeight) {
        const z = this.#zoom
        if (typeof z === 'number' && !isNaN(z) && z > 0) return size.width * z
        if (z === 'fit-page' && size.height > 0)
            return Math.min(viewWidth, viewHeight * size.width / size.height)
        return viewWidth
    }
    constructor() {
        super()

        const sheet = new CSSStyleSheet()
        this.#root.adoptedStyleSheets = [sheet]
        sheet.replaceSync(`:host {
            width: 100%;
            height: 100%;
            display: flex;
            /* safe: страница крупнее окна (масштаб) прокручивается, а не
               обрезается слева и сверху, как при обычном center. */
            justify-content: safe center;
            align-items: safe center;
            overflow: auto;
        }`)

        this.addEventListener('scroll', () => {
            if (!this.scrolled || !this.#strip) return
            // Позиция — сразу (её читает #place при подгрузке страниц), relocate и
            // подгрузка — раз за кадр: событий scroll на жест бывает больше кадров.
            this.#position = scrollPosition(this.#heights, this.scrollTop)
            if (this.#scrollRAF == null) this.#scrollRAF = requestAnimationFrame(() => {
                this.#scrollRAF = null
                if (!this.scrolled || !this.#strip) return
                this.#reportLocation('scroll')
                this.#syncPages()
            })
        }, { passive: true })
        this.#observer.observe(this)
    }
    attributeChangedCallback(name, oldValue, value) {
        if (oldValue === value) return
        if (name === 'flow' && this.book) {
            // Читалка ставит flow до первого показа книги: перестраивать нечего,
            // первый goTo сам соберёт нужный режим на позиции возврата.
            if (this.#index < 0 && !this.#strip) return
            const position = this.#strip ? this.#position : { index: Math.max(0, this.index), offset: this.#position.offset }
            this.#ready = this.#changeFlow(position)
            return
        }
        switch (name) {
            case 'zoom':
                this.#zoom = value !== 'fit-width' && value !== 'fit-page'
                    ? parseFloat(value) : value
                this.#render()
                break
        }
    }
    async #createFrame({ index, src: srcOption }, parent = this.#root) {
        const srcOptionIsString = typeof srcOption === 'string'
        const src = srcOptionIsString ? srcOption : srcOption?.src
        const onZoom = srcOptionIsString ? null : srcOption?.onZoom
        const element = document.createElement('div')
        element.setAttribute('dir', 'ltr')
        const iframe = document.createElement('iframe')
        iframe.dataset.index = index
        element.append(iframe)
        Object.assign(iframe.style, {
            border: '0',
            display: 'none',
            overflow: 'hidden',
        })
        // `allow-scripts` is needed for events because of WebKit bug
        // https://bugs.webkit.org/show_bug.cgi?id=218086
        iframe.setAttribute('sandbox', 'allow-same-origin allow-scripts')
        iframe.setAttribute('scrolling', 'no')
        iframe.setAttribute('part', 'filter')
        parent.append(element)
        if (!src) return { blank: true, element, iframe }
        return new Promise(resolve => {
            const signal = this.#frameController.signal
            const cancel = () => { element.remove(); resolve({ element, iframe, cancelled: true }) }
            signal.addEventListener('abort', cancel, { once: true })
            iframe.addEventListener('load', () => {
                signal.removeEventListener('abort', cancel)
                if (signal.aborted) return
                const doc = iframe.contentDocument
                this.#attachScrollInput(doc)
                this.dispatchEvent(new CustomEvent('load', { detail: { doc, index } }))
                const { width, height } = getViewport(doc, this.defaultViewport)
                resolve({
                    element, iframe,
                    width: parseFloat(width),
                    height: parseFloat(height),
                    onZoom, getSize: srcOption?.getSize,
                })
            }, { once: true })
            iframe.src = src
        })
    }
    #render(side = this.#side) {
        // После удаления ResizeObserver ещё может прислать нулевой размер.
        if (!this.isConnected) return
        if (this.scrolled) { this.#layoutStrip(); return }
        if (!side) return
        for (const frame of [this.#left, this.#right, this.#center]) {
            if (frame?.getSize) Object.assign(frame, frame.getSize())
        }
        const left = this.#left ?? {}
        const right = this.#center ?? this.#right ?? {}
        const target = side === 'left' ? left : right
        const { width, height } = this.getBoundingClientRect()
        const portrait = this.spread !== 'both' && this.spread !== 'portrait'
            && height > width
        this.#portrait = portrait
        const blankWidth = left.width ?? right.width ?? 0
        const blankHeight = left.height ?? right.height ?? 0

        const scale = typeof this.#zoom === 'number' && !isNaN(this.#zoom)
            ? this.#zoom
            : (this.#zoom === 'fit-width'
                ? (portrait || this.#center
                    ? width / (target.width ?? blankWidth)
                    : width / ((left.width ?? blankWidth) + (right.width ?? blankWidth)))
                : (portrait || this.#center
                    ? Math.min(
                        width / (target.width ?? blankWidth),
                        height / (target.height ?? blankHeight))
                    : Math.min(
                        width / ((left.width ?? blankWidth) + (right.width ?? blankWidth)),
                        height / Math.max(
                            left.height ?? blankHeight,
                            right.height ?? blankHeight)))
            ) || 1
        this.#scale = scale

        const transform = frame => {
            let { element, iframe, width, height, blank, onZoom } = frame
            const doc = iframe?.contentDocument
            // При закрытии книги фрейм уже может быть отсоединён.
            if (!iframe?.isConnected || !doc) return
            if (onZoom) frame.pending = this.#zoomFrame(frame, doc, scale)
            const iframeScale = onZoom ? scale : 1
            Object.assign(iframe.style, {
                width: `${width * iframeScale}px`,
                height: `${height * iframeScale}px`,
                transform: onZoom ? 'none' : `scale(${scale})`,
                transformOrigin: 'top left',
                display: blank ? 'none' : 'block',
            })
            Object.assign(element.style, {
                width: `${(width ?? blankWidth) * scale}px`,
                height: `${(height ?? blankHeight) * scale}px`,
                overflow: 'hidden',
                display: 'block',
                flexShrink: '0',
                marginBlock: 'auto',
            })
            if (portrait && frame !== target) {
                element.style.display = 'none'
            }
        }
        if (this.#center) {
            transform(this.#center)
        } else {
            transform(left)
            transform(right)
        }
    }
    async #showSpread({ left, right, center, side }) {
        const generation = this.#generation
        for (const frame of [this.#left, this.#right, this.#center]) if (frame) this.#release(frame)
        this.#root.replaceChildren()
        this.#left = null
        this.#right = null
        this.#center = null
        if (center) {
            const frame = await this.#createFrame(center)
            if (generation !== this.#generation) return
            this.#center = frame
            this.#side = 'center'
            this.#render()
        } else {
            const leftFrame = await this.#createFrame(left)
            if (generation !== this.#generation) return
            this.#left = leftFrame
            const rightFrame = await this.#createFrame(right)
            if (generation !== this.#generation) return
            this.#right = rightFrame
            this.#side = this.#left.blank ? 'right'
                : this.#right.blank ? 'left' : side
            this.#render()
        }
    }
    #goLeft() {
        if (this.#center || this.#left?.blank) return
        if (this.#portrait && this.#left?.element?.style?.display === 'none') {
            this.#side = 'left'
            this.#render()
            this.#reportLocation('page')
            return true
        }
    }
    #goRight() {
        if (this.#center || this.#right?.blank) return
        if (this.#portrait && this.#right?.element?.style?.display === 'none') {
            this.#side = 'right'
            this.#render()
            this.#reportLocation('page')
            return true
        }
    }
    open(book) {
        this.book = book
        const { rendition } = book
        this.spread = rendition?.spread
        this.defaultViewport = rendition?.viewport

        const rtl = book.dir === 'rtl'
        const ltr = !rtl
        this.rtl = rtl

        if (rendition?.spread === 'none')
            this.#spreads = book.sections.map(section => ({ center: section }))
        else this.#spreads = book.sections.reduce((arr, section, i) => {
            const last = arr[arr.length - 1]
            const { pageSpread } = section
            const newSpread = () => {
                const spread = {}
                arr.push(spread)
                return spread
            }
            if (pageSpread === 'center') {
                const spread = last.left || last.right ? newSpread() : last
                spread.center = section
            }
            else if (pageSpread === 'left') {
                const spread = last.center || last.left || ltr && i ? newSpread() : last
                spread.left = section
            }
            else if (pageSpread === 'right') {
                const spread = last.center || last.right || rtl && i ? newSpread() : last
                spread.right = section
            }
            else if (ltr) {
                if (last.center || last.right) newSpread().left = section
                else if (last.left || !i) last.right = section
                else last.left = section
            }
            else {
                if (last.center || last.left) newSpread().right = section
                else if (last.right || !i) last.left = section
                else last.right = section
            }
            return arr
        }, [{}])
    }
    async setPDFOptions(options) {
        this.book.onPDFCropReady = () => this.#render()
        await this.book.setPDFOptions(options)
        this.style.color = options.fg || 'inherit'
        this.#render()
    }
    get index() {
        if (this.scrolled) return this.#position.index
        const spread = this.#spreads[this.#index]
        if (!spread) return -1
        const section = spread?.center ?? (this.#side === 'left'
            ? spread.left ?? spread.right : spread.right ?? spread.left)
        return this.book.sections.indexOf(section)
    }
    #reportLocation(reason) {
        if (!this.scrolled && this.index !== this.#position.index)
            this.#position = { index: this.index, offset: 0 }
        this.book.prefetch?.(this.scrolled
            ? visiblePages(this.#heights, this.scrollTop, this.#viewHeight || this.clientHeight, 0) : [this.index])
        this.dispatchEvent(new CustomEvent('relocate', { detail:
            { reason, range: null, index: this.index, fraction: this.scrolled ? this.#position.offset : 0, size: this.scrolled ? (this.atEnd ? this.book.sections.length - this.index - this.#position.offset : 0) : 1 } }))
    }
    getSpreadOf(section) {
        const spreads = this.#spreads
        for (let index = 0; index < spreads.length; index++) {
            const { left, right, center } = spreads[index]
            if (left === section) return { index, side: 'left' }
            if (right === section) return { index, side: 'right' }
            if (center === section) return { index, side: 'center' }
        }
    }
    async goToSpread(index, side, reason) {
        if (index < 0 || index > this.#spreads.length - 1) return
        if (index === this.#index) {
            this.#side = side
            this.#render(side)
            this.#reportLocation(reason)
            return
        }
        this.#index = index
        const generation = this.#generation
        const spread = this.#spreads[index]
        if (spread.center) {
            const index = this.book.sections.indexOf(spread.center)
            const src = await spread.center?.load?.()
            if (generation !== this.#generation) return
            await this.#showSpread({ center: { index, src } })
        } else {
            const indexL = this.book.sections.indexOf(spread.left)
            const indexR = this.book.sections.indexOf(spread.right)
            const srcL = await spread.left?.load?.()
            const srcR = await spread.right?.load?.()
            const left = { index: indexL, src: srcL }
            const right = { index: indexR, src: srcR }
            if (generation !== this.#generation) return
            await this.#showSpread({ left, right, side })
        }
        if (generation === this.#generation) this.#reportLocation(reason)
    }
    // Колесо из iframe передаём контейнеру; касания используют нативную цепочку прокрутки.
    #attachScrollInput(doc) {
        doc.addEventListener('wheel', e => {
            // Ctrl + колесо (и щипок тачпада, он приходит так же) — масштаб
            // страницы, а не всего окна браузера вместе с панелями.
            if (e.ctrlKey || e.metaKey) {
                e.preventDefault()
                this.dispatchEvent(new CustomEvent('zoom-request', { detail: { wheel: e.deltaY } }))
                return
            }
            if (!this.scrolled) return
            e.preventDefault()
            this.scrollBy(0, e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? this.clientHeight : 1))
        }, { passive: false })
        doc.addEventListener('keydown', e => this.handleScrollKey(e))
        this.#attachPinch(doc)
    }
    // Щипок двумя пальцами: пока пальцы на экране — превью трансформацией,
    // отпустили — один запрос масштаба (перерисовка страниц PDF дорогая).
    #attachPinch(doc) {
        const dist = t => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY)
        const target = () => this.scrolled ? this.#strip : this.#root.firstElementChild
        doc.addEventListener('touchstart', e => {
            if (e.touches.length !== 2) return
            const d = dist(e.touches)
            if (d < 10) return
            const host = this.getBoundingClientRect()
            const frame = doc.defaultView.frameElement?.getBoundingClientRect() ?? host
            const cx = (e.touches[0].clientX + e.touches[1].clientX) / 2 + frame.left
            const cy = (e.touches[0].clientY + e.touches[1].clientY) / 2 + frame.top
            this.#pinch = { d, ratio: 1, el: target(), ox: cx - host.left + this.scrollLeft, oy: cy - host.top + this.scrollTop }
            this.pinchUntil = Infinity
        }, { passive: true })
        doc.addEventListener('touchmove', e => {
            const p = this.#pinch
            if (!p || e.touches.length !== 2) return
            e.preventDefault() // иначе браузер масштабирует всю страницу
            p.ratio = Math.max(0.3, Math.min(4, dist(e.touches) / p.d))
            if (p.el) Object.assign(p.el.style, { transform: `scale(${p.ratio})`, transformOrigin: this.scrolled ? `${p.ox}px ${p.oy}px` : 'center' })
        }, { passive: false })
        const end = e => {
            const p = this.#pinch
            if (!p || e.touches.length > 1) return
            this.#pinch = null
            this.pinchUntil = Date.now() + 400
            if (p.el) p.el.style.transform = ''
            if (Math.abs(p.ratio - 1) > 0.04)
                this.dispatchEvent(new CustomEvent('zoom-request', { detail: { ratio: p.ratio } }))
        }
        doc.addEventListener('touchend', end, { passive: true })
        doc.addEventListener('touchcancel', end, { passive: true })
    }
    handleScrollKey(e) {
        if (!this.scrolled || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey
            || e.target?.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target?.tagName)) return false
        const steps = { ArrowDown: 48, ArrowRight: 48, ArrowUp: -48, ArrowLeft: -48,
            PageDown: this.clientHeight, PageUp: -this.clientHeight,
            ' ': this.clientHeight * (e.shiftKey ? -1 : 1) }
        if (!(e.key in steps)) return false
        e.preventDefault(); this.scrollBy(0, steps[e.key]); return true
    }
    #release(frame) {
        const doc = frame.iframe.contentDocument
        frame.element.remove()
        const clear = () => {
            if (doc?.pdfImageURL) {
                URL.revokeObjectURL(doc.pdfImageURL)
                doc.pdfImageURL = null
            }
            for (const canvas of doc?.querySelectorAll('canvas') ?? []) { canvas.width = 0; canvas.height = 0 }
        }
        if (frame.pending) frame.pending.finally(clear).catch(() => {})
        else clear()
    }
    async #changeFlow(position) {
        const generation = ++this.#generation
        this.#frameController.abort()
        this.#frameController = new AbortController()
        cancelAnimationFrame(this.#scrollRAF)
        this.#scrollRAF = null
        for (const frame of [this.#left, this.#right, this.#center]) if (frame) this.#release(frame)
        for (const frame of this.#frames.values()) this.#release(frame)
        this.#frames.clear()
        this.#root.replaceChildren()
        this.#strip = null
        this.#left = this.#right = this.#center = null
        this.#index = -1
        this.#position = position
        if (!this.scrolled) {
            this.style.display = ''; this.scrollTop = 0
            const spread = this.getSpreadOf(this.book.sections[position.index])
            await this.goToSpread(spread.index, spread.side, 'flow')
            return
        }
        // Размеры из метаданных: невидимым страницам canvas не нужен.
        const estimate = await this.book.sections[0].getSize?.() ?? { width: 1000, height: 1400 }
        const sizes = this.book.sections.map(section => section.getCachedSize?.() ?? estimate)
        if (generation !== this.#generation) return
        this.style.display = 'block'
        this.#strip = document.createElement('div')
        this.#slots = sizes.map((size, index) => {
            const slot = document.createElement('div')
            slot.dataset.index = index
            slot.size = size
            slot.style.cssText = 'position:relative;overflow:hidden;box-sizing:border-box;border-bottom:1px solid color-mix(in srgb,currentColor 30%,transparent)'
            this.#strip.append(slot)
            return slot
        })
        this.#root.append(this.#strip)
        this.#layoutStrip()
        // Даём восстановлению позиции выбрать целевую страницу до рендера.
        setTimeout(() => { if (generation === this.#generation) this.#syncPages() }, 0)
        this.#reportLocation('flow')
    }
    #place() {
        this.scrollTop = this.#heights.slice(0, this.#position.index).reduce((a, b) => a + b, 0)
            + this.#heights[this.#position.index] * this.#position.offset
    }
    #layoutStrip() {
        // Асинхронная загрузка страницы может завершиться после удаления читалки.
        if (!this.isConnected || !this.#strip) return
        // Размеры вьюпорта читаем один раз: чтение clientWidth после записи
        // style.height заставляло пересчитывать раскладку на каждом из сотен слотов.
        const viewWidth = this.clientWidth
        this.#viewHeight = this.clientHeight
        let stripWidth = viewWidth
        const current = this.#position.index
        this.#heights = this.#slots.map((slot, index) => {
            const frame = this.#frames.get(index)
            const size = frame?.getSize?.() ?? this.book.sections[index].getCachedSize?.() ?? slot.size
            slot.size = size
            // Масштаб (serg/tasks#3274): по ширине окна, страница целиком или
            // число. Страница уже окна — по центру, шире — горизонтальная прокрутка.
            const width = Math.max(40, Math.round(this.#stripPageWidth(size, viewWidth, this.#viewHeight)))
            if (width > stripWidth) stripWidth = width
            const scale = width / size.width
            if (index === current) this.#scale = scale
            const height = size.height * scale + 1
            const css = `${height}px`
            if (slot.style.height !== css) slot.style.height = css
            const w = `${width}px`
            if (slot.style.width !== w) { slot.style.width = w; slot.style.marginInline = 'auto' }
            const doc = frame?.iframe.contentDocument
            if (frame?.iframe.isConnected && doc) {
                Object.assign(frame.iframe.style, { display: 'block', width: `${width}px`, height: `${height - 1}px` })
                if (frame.onZoom) frame.pending = this.#zoomFrame(frame, doc, scale)
                else Object.assign(frame.iframe.style, { width: `${size.width}px`, height: `${size.height}px`, transform: `scale(${scale})`, transformOrigin: 'top left' })
            }
            return height
        })
        const sw = stripWidth > viewWidth ? `${stripWidth}px` : ''
        if (this.#strip.style.width !== sw) this.#strip.style.width = sw
        this.#stripHeight = this.#heights.reduce((a, b) => a + b, 0)
        this.#place()
        // Масштаб поменяли — страница шире окна встаёт по центру, а не прижатой влево.
        const key = String(this.#zoom)
        if (key !== this.#zoomKey) {
            this.#zoomKey = key
            this.scrollLeft = Math.max(0, (stripWidth - viewWidth) / 2)
        }
    }
    #syncPages() {
        if (this.#loading) return this.#loading
        const generation = this.#generation
        this.#loading = this.#updatePages().finally(() => {
            this.#loading = null
            if (generation !== this.#generation && this.#strip) this.#syncPages()
        })
        return this.#loading
    }
    async #updatePages() {
        const generation = this.#generation
        try {
            // Последовательно: быстрый скролл не копит тяжёлые рендеры.
            while (generation === this.#generation && this.#strip) {
                const wanted = visiblePages(this.#heights, this.scrollTop, this.#viewHeight || this.clientHeight)
                for (const [index, frame] of this.#frames) if (!wanted.includes(index)) {
                    this.#release(frame); this.#frames.delete(index)
                }
                const index = wanted.sort((a, b) => Math.abs(a - this.#position.index) - Math.abs(b - this.#position.index)).find(i => !this.#frames.has(i))
                if (index == null) break
                const src = await this.book.sections[index].load()
                if (generation !== this.#generation) break
                const frame = await this.#createFrame({ index, src }, this.#slots[index])
                if (generation !== this.#generation) { this.#release(frame); break }
                this.#frames.set(index, frame)
                this.#layoutStrip()
                await frame.pending
            }
        } catch (error) { console.error('PDF scroll:', error) }
    }
    async goToFraction(fraction) {
        const { index, offset } = fromFraction(fraction, this.book.sections.length)
        return this.goTo({ index, anchor: offset })
    }
    async select(target) {
        const resolved = await target
        await this.goTo({ ...resolved, select: true })
    }
    async goTo(target) {
        const { book } = this
        await this.#ready
        const resolved = typeof target === 'number' ? { index: target } : await target
        const section = book.sections[resolved.index]
        if (!section) return
        if (this.scrolled) {
            if (!this.#strip) await this.#changeFlow({ index: resolved.index, offset: 0 })
            const anchor = resolved.anchor
            this.#position = { index: resolved.index, offset: typeof anchor === 'number' ? anchor : 0 }
            this.#place()
            await this.#syncPages()
            const frame = this.#frames.get(resolved.index)
            if (typeof anchor === 'function' && frame) {
                const value = anchor(frame.iframe.contentDocument)
                if (typeof value === 'number') this.#position.offset = value
                else if (value?.getBoundingClientRect) {
                    this.#position.offset = value.getBoundingClientRect().top / this.#heights[resolved.index]
                    if (resolved.select && value.cloneRange) {
                        const selection = frame.iframe.contentDocument.getSelection()
                        selection.removeAllRanges(); selection.addRange(value)
                    }
                }
                this.#place()
            }
            this.#reportLocation('navigation')
            return
        }
        this.#position = { index: resolved.index, offset: 0 }
        const { index, side } = this.getSpreadOf(section)
        await this.goToSpread(index, side)
    }
    async next() {
        if (this.scrolled) {
            await this.#ready
            if (!this.#strip) return this.goTo(0)
            this.scrollBy(0, this.clientHeight); return
        }
        const s = this.rtl ? this.#goLeft() : this.#goRight()
        if (!s) return this.goToSpread(this.#index + 1, this.rtl ? 'right' : 'left', 'page')
    }
    async prev() {
        if (this.scrolled) { this.scrollBy(0, -this.clientHeight); return }
        const s = this.rtl ? this.#goRight() : this.#goLeft()
        if (!s) return this.goToSpread(this.#index - 1, this.rtl ? 'left' : 'right', 'page')
    }
    getContents() {
        return Array.from(this.#root.querySelectorAll('iframe'), frame => ({
            doc: frame.contentDocument, index: Number(frame.dataset.index),
            // TODO: index, overlayer
        }))
    }
    destroy() {
        this.#strip = null
        this.#generation++
        this.#frameController.abort()
        cancelAnimationFrame(this.#scrollRAF)
        this.#scrollRAF = null
        for (const frame of [this.#left, this.#right, this.#center]) if (frame) this.#release(frame)
        for (const frame of this.#frames.values()) this.#release(frame)
        this.#frames.clear()
        this.#observer.unobserve(this)
    }
}

customElements.define('foliate-fxl', FixedLayout)
