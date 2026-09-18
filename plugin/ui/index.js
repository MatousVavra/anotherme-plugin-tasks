function tasksPlugin() {
    return {
        view: 'today',
        tasks: [],
        loading: false,
        quickAdd: '',
        dialogOpen: false,
        editing: null,
        saving: false,
        deleteMode: null,
        formTitle: '',
        formDue: '',
        formNotes: '',
        doneOpen: false,

        init() {
            const cfg = (AM.plugin && AM.plugin.config) || {};
            if (['today', 'inbox', 'all'].includes(cfg.default_view)) {
                this.view = cfg.default_view;
            }
            this.loadTasks();
        },

        todayStr(d = new Date()) {
            const offset = d.getTimezoneOffset();
            return new Date(d.getTime() - offset * 60000).toISOString().split('T')[0];
        },

        async loadTasks() {
            this.loading = true;
            try {
                const resp = await AM.fetch('/plugins/tasks/tasks');
                if (!resp || !resp.ok) { AM.toast('Failed to load tasks', 'error'); return; }
                const data = await resp.json();
                this.tasks = data.tasks || [];
            } catch (e) {
                console.error('Failed to load tasks', e);
                AM.toast('Failed to load tasks', 'error');
            } finally { this.loading = false; }
        },

        switchView(v) {
            this.view = v;
        },

        // --- Sections ---

        sections() {
            const today = this.todayStr();
            const open = this.tasks.filter(t => t.status !== 'done');
            const done = this.tasks
                .filter(t => t.status === 'done')
                .sort((a, b) => (b.completed_at || '').localeCompare(a.completed_at || ''));
            const out = [];
            if (this.view === 'done') {
                if (done.length) out.push({ key: 'done', label: 'Done', tasks: done.slice(0, 20) });
                return out;
            }
            const overdue = open.filter(t => t.due_date && t.due_date < today);
            const todays = open.filter(t => t.due_date === today);
            const upcoming = open.filter(t => t.due_date && t.due_date > today);
            const inbox = open.filter(t => !t.due_date);
            if (this.view === 'today') {
                if (overdue.length) out.push({ key: 'overdue', label: 'Overdue', tasks: overdue });
                if (todays.length) out.push({ key: 'today', label: 'Today', tasks: todays });
                if (inbox.length) out.push({ key: 'inbox', label: 'Inbox', tasks: inbox });
            } else if (this.view === 'inbox') {
                if (inbox.length) out.push({ key: 'inbox', label: 'Inbox', tasks: inbox });
            } else {
                if (overdue.length) out.push({ key: 'overdue', label: 'Overdue', tasks: overdue });
                if (todays.length) out.push({ key: 'today', label: 'Today', tasks: todays });
                if (upcoming.length) out.push({ key: 'upcoming', label: 'Upcoming', tasks: upcoming });
                if (inbox.length) out.push({ key: 'inbox', label: 'Inbox', tasks: inbox });
                if (done.length) out.push({ key: 'done', label: 'Done', tasks: done.slice(0, 20), collapsible: true });
            }
            return out;
        },

        isOverdue(t) {
            return t.due_date && t.due_date < this.todayStr() && t.status !== 'done';
        },

        dueLabel(dateStr) {
            if (!dateStr) return '';
            const today = this.todayStr();
            if (dateStr === today) return 'today';
            if (dateStr === this.todayStr(new Date(Date.now() + 86400000))) return 'tomorrow';
            return new Date(dateStr + 'T00:00:00')
                .toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
        },

        // --- Quick add ---

        parseQuickAdd(text) {
            let due = null;
            const simple = [
                [/\btoday\b/i, 0],
                [/\btomorrow\b/i, 1],
            ];
            for (const [re, days] of simple) {
                if (re.test(text)) {
                    text = text.replace(re, ' ');
                    due = this.dateInDays(days);
                    break;
                }
            }
            if (!due) {
                const days = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'];
                for (let i = 0; i < days.length; i++) {
                    const re = new RegExp('\\b' + days[i] + '\\b', 'i');
                    if (re.test(text)) {
                        text = text.replace(re, ' ');
                        let ahead = (i - new Date().getDay() + 7) % 7;
                        if (ahead === 0) ahead = 7;
                        due = this.dateInDays(ahead);
                        break;
                    }
                }
            }
            return { title: text.replace(/\s+/g, ' ').trim(), due_date: due };
        },

        dateInDays(n) {
            return this.todayStr(new Date(Date.now() + n * 86400000));
        },

        async quickAddSubmit() {
            const raw = this.quickAdd.trim();
            if (!raw) return;
            const parsed = this.parseQuickAdd(raw);
            if (!parsed.title) { this.quickAdd = ''; return; }
            const body = { title: parsed.title };
            if (parsed.due_date) body.due_date = parsed.due_date;
            try {
                const resp = await AM.fetch('/plugins/tasks/tasks', { method: 'POST', body });
                if (!resp || !resp.ok) {
                    AM.toast('Failed to add task (HTTP ' + (resp ? resp.status : '?') + ')', 'error');
                    return;
                }
                AM.toast(parsed.due_date ? 'Task added — due ' + parsed.due_date : 'Task added', 'success');
                this.quickAdd = '';
                await this.loadTasks();
            } catch (e) {
                AM.toast('Failed to add task', 'error');
            }
        },

        // --- Task actions ---

        async toggleTask(t) {
            const newStatus = t.status === 'done' ? 'todo' : 'done';
            try {
                const resp = await AM.fetch('/plugins/tasks/tasks/' + t.id, {
                    method: 'PUT', body: { status: newStatus },
                });
                if (!resp || !resp.ok) { AM.toast('Failed to update task', 'error'); return; }
                AM.toast(newStatus === 'done' ? 'Task completed' : 'Task reopened', 'success');
                await this.loadTasks();
            } catch (e) {
                AM.toast('Failed to update task', 'error');
            }
        },

        openEdit(t) {
            this.editing = t;
            this.deleteMode = null;
            this.formTitle = t.title || '';
            this.formDue = t.due_date || '';
            this.formNotes = t.notes || '';
            this.dialogOpen = true;
        },

        closeDialog() {
            this.dialogOpen = false;
            this.editing = null;
            this.deleteMode = null;
        },

        async saveTask() {
            const title = this.formTitle.trim();
            if (!title) return;
            this.saving = true;
            try {
                const body = { title };
                if (this.formDue) body.due_date = this.formDue;
                if (this.formNotes.trim()) body.notes = this.formNotes.trim();
                const resp = this.editing
                    ? await AM.fetch('/plugins/tasks/tasks/' + this.editing.id, { method: 'PUT', body })
                    : await AM.fetch('/plugins/tasks/tasks', { method: 'POST', body });
                if (!resp || !resp.ok) {
                    AM.toast('Failed to save task (HTTP ' + (resp ? resp.status : '?') + ')', 'error');
                    return;
                }
                AM.toast(this.editing ? 'Task updated' : 'Task saved', 'success');
                this.closeDialog();
                await this.loadTasks();
            } catch (e) {
                AM.toast('Failed to save task', 'error');
            } finally { this.saving = false; }
        },

        async deleteTask() {
            if (!this.editing) return;
            if (this.deleteMode !== 'confirm') { this.deleteMode = 'confirm'; return; }
            this.saving = true;
            try {
                const resp = await AM.fetch('/plugins/tasks/tasks/' + this.editing.id, { method: 'DELETE' });
                if (!resp || !resp.ok) { AM.toast('Failed to delete task', 'error'); return; }
                AM.toast('Task deleted', 'success');
                this.closeDialog();
                await this.loadTasks();
            } catch (e) {
                AM.toast('Failed to delete task', 'error');
            } finally { this.saving = false; }
        },
    };
}
