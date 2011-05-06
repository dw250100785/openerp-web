/*---------------------------------------------------------
 * OpenERP base_graph
 *---------------------------------------------------------*/

openerp.base_graph = function (openerp) {
QWeb.add_template('/base_graph/static/src/xml/base_graph.xml');
openerp.base.views.add('graph', 'openerp.base_graph.GraphView');
openerp.base_graph.GraphView = openerp.base.Controller.extend({

	init: function(view_manager, session, element_id, dataset, view_id) {

        this._super(session, element_id);
        this.view_manager = view_manager;
        this.dataset = dataset;
        this.model = this.dataset.model;
        this.view_id = view_id;
    },
    do_show: function () {
        // TODO: re-trigger search
        this.$element.show();
    },
    do_hide: function () {
        this.$element.hide();
    },
    start: function() {
        this.rpc("/base_graph/graphview/load", {"model": this.model, "view_id": this.view_id}, this.on_loaded);
    },
    on_loaded: function(data) {
        var self = this;
        this.fields_view = data.fields_view;
        this.name = this.fields_view.name || this.fields_view.arch.attrs.string;
        this.view_id = this.fields_view.view_id;
        this.chart = this.fields_view.arch.attrs.type || 'pie';
        this.fields = this.fields_view.fields;
        this.chart_info_fields = [];
        this.operator_field = '';
        this.group_field = '';
        this.orientation = this.fields_view.arch.attrs.orientation || '';

        for(fld in this.fields_view.arch.children) {
            if (this.fields_view.arch.children[fld].attrs.operator) {
                this.operator_field = this.fields_view.arch.children[fld].attrs.name;
            }
            else if (this.fields_view.arch.children[fld].attrs.group) {
                this.group_field = this.fields_view.arch.children[fld].attrs.name;
            }
            else {
                this.chart_info_fields.push(this.fields_view.arch.children[fld].attrs.name);
            }
        }
        if(this.operator_field == ''){
            this.operator_field = this.chart_info_fields[1];
        }
        this.chart_info = this.chart_info_fields[0];
        this.load_chart();
    },

    load_chart: function(result) {
        var self = this;
        if(result){
            self.schedule_chart(result);
        }else{
            this.dataset.read_ids(
                this.dataset.ids,
                {},
                function(result) {
                    self.schedule_chart(result);
                }
            )
        }
    },

    schedule_chart: function(result) {
        this.$element.html(QWeb.render("GraphView", {"fields_view": this.fields_view, "chart": this.chart}));

        if (result.length){
            for(res in result) {
                for(fld in result[res]) {
                    if (typeof result[res][fld] == 'object') {
                        result[res][fld] = result[res][fld][result[res][fld].length - 1];
                    }
                }
            }

            if(this.chart == 'bar') {
                return this.schedule_bar(result);
            } else if(this.chart == "pie") {
                return this.schedule_pie(result);
            }
        }
    },

    schedule_bar: function(result) {
        var self = this;
        var view_chart = '';
        var xystr = {};
        var xyname = {};
        var res = [];
        var group_list = [];
        var opration_fld = {};

        for (i in result){
            var gen_key = result[i][self.chart_info_fields]+"_"+result[i][self.group_field]
            if (opration_fld[gen_key] == undefined){
                var map_val = {}
                map_val[self.operator_field] = result[i][self.operator_field];
                map_val[self.chart_info_fields] = result[i][self.chart_info_fields];
                if (this.group_field){
                    map_val[self.group_field] = result[i][self.group_field].replace(" ", "_");
                }else{
                    map_val[self.group_field] = result[i][self.group_field];
                }
                opration_fld[gen_key] = map_val;
            }else{
                map_val = opration_fld[gen_key];
                map_val[self.operator_field] = map_val[self.operator_field] + result[i][self.operator_field];
                opration_fld[gen_key] = map_val;
            }
        }

        result = []
        for (i in opration_fld){
            result.push(opration_fld[i]);
        }

        var COLOR_PALETTE = ['#ff8e00', '#ff0000', '#b0008c', '#9000ff', '#0078ff', '#00ff00', '#e6ff00', '#ffff00',
                     '#905000', '#9b0000', '#840067', '#9abe00', '#ffc900', '#510090', '#0000c9', '#009b00',
                     '#75507b', '#3465a4', '#73d216', '#c17d11', '#edd400', '#fcaf3e', '#ef2929', '#ff00c9',
                     '#ad7fa8', '#729fcf', '#8ae234', '#e9b96e', '#fce94f', '#f57900', '#cc0000', '#d400a8'];

        if(this.group_field){
            view_chart = this.orientation == 'horizontal'? 'stackedBarH' : 'stackedBar';
        }else{
            view_chart = this.orientation == 'horizontal'? 'barH' : 'bar';
        }

        for (i in result){
            if (this.group_field){
                var newkey =result[i][this.group_field].replace(" ", "_");
            }else{
                var newkey =result[i][this.group_field];
            }
            if (jQuery.inArray(newkey, group_list) == -1){
                group_list.push(newkey);
            }
        }

        for (i in result){
            var xystring = result[i][this.chart_info_fields];
             if (this.group_field){
                var newkey =result[i][this.group_field].replace(" ", "_");
            }else{
                var newkey =result[i][this.group_field];
            }
            if (xystr[xystring] == undefined){
                xyname = {};
                xyname['name'] = xystring;
                for (j in group_list){
                    xyname[group_list[j]] = 0.0001;
                }
                xyname[newkey] = result[i][this.operator_field];
                xystr[xystring] = xyname;
            }
            else{
                xyname = {};
                xyname = xystr[xystring];
                xyname[newkey] = result[i][this.operator_field];
                xystr[xystring] = xyname;
            }
        }

        for (i in xystr){
            res.push(xystr[i]);
        }

        //for legend color
        var grp_color = [];
        for (i in group_list){
            var legend = {};
            if (group_list[i] == undefined){
                legend['text'] = self.fields[self.operator_field]['string']
            }else{
                legend['text'] = group_list[i];
            }
            legend['color'] = COLOR_PALETTE[i];
            grp_color.push(legend);
        }

        var temp_ax = {};
        var oth_ax = {};

        temp_ax['template'] = "#name#";
        temp_ax['lines'] = true;

        oth_ax['lines'] = true;

        if (this.orientation == 'horizontal'){
             var x_ax = oth_ax;
             var y_ax = temp_ax;
        }else{
             var x_ax = temp_ax;
             var y_ax = oth_ax;
        }

        var bar_chart = new dhtmlxchartChart({
            view: view_chart,
            container: "barchart",
            value:"#"+group_list[0]+"#",
            gradient: "3d",
            border: false,
            width: 30,
            color:grp_color[0]['color'],
            origin:0,
            xAxis: x_ax,
            yAxis: y_ax,
            legend: {
                values: grp_color,
                align:"right",
                valign:"top",
                marker:{
                    type:"round",
                    width:12
                },
            }
        });
        for (var i = 1; i<group_list.length;i++){
            bar_chart.addSeries({
                value: "#"+grp_color[i]['text']+"#",
                color: grp_color[i]['color']
            });
        }
        bar_chart.parse(res,"json");

    },

    schedule_pie: function(result) {
        var chart =  new dhtmlxchartChart({
            view:"pie",
            container:"piechart",
            value:"#"+this.operator_field+"#",
            color:"#d2ed7e",
            label:"#"+this.chart_info_fields[0]+"#",
            pieInnerText:"<b>#"+this.operator_field+"#</b>",
            gradient:"3d",
            legend: {
                width: 100,
                align:"right",
                valign:"top",
                marker:{
                    type:"round",
                    width:12
                },
                template:"#"+this.chart_info_fields[0]+"#"
            }
        });
        chart.parse(result,"json");
    },

    do_search: function(domains, contexts, groupbys) {
        var self = this;

        this.rpc('/base/session/eval_domain_and_context', {
            domains: domains,
            contexts: contexts,
            group_by_seq: groupbys
        }, function (results) {
            // TODO: handle non-empty results.group_by with read_group
            if(contexts[0]['group_by']){
                self.chart_info_fields = contexts[0]['group_by'];
            }
            else{
                self.chart_info_fields = self.chart_info;
            }
            self.dataset.context = self.context = results.context;
            self.dataset.domain = self.domain = results.domain;
            self.dataset.read_slice(self.fields, 0, self.limit,function(result){
                self.load_chart(result);
            });
        });

    },

});

// here you may tweak globals object, if any, and play with on_* or do_* callbacks on them

};

// vim:et fdc=0 fdl=0:
